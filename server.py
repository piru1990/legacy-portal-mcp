"""MCP: STOD (portal interno de Acme, ASP.NET WebForms).

STOD no tiene API: se ingresa como en el navegador (txtUsuario/txtPass en Portal.aspx) y se
lee el HTML. La credencial viene del Administrador de credenciales de Windows (_stod_env.py).

Lectura (solo GET / navegación del grid, no guardan nada):
  menu(texto)            busca en el menú del usuario (opciones y reportes) con su ruta.
  page(path)             lee una página del portal (GET) y devuelve su texto y sus campos.
  usuarios(texto)        busca usuarios en Administración > Usuarios (nombre, apellido o usuario).
  usuario(usuario)       ficha de un usuario: depto, correo, NIT, rol, banderas (sin contraseña).
  roles(texto)           lista los roles (un usuario tiene UN rol; el menú sale del rol).
  permisos_rol(rol)      menús que da un rol.
  proponer_usuario(nombre, apellido)   logins libres con la convención inicial+apellido.
  comparar_usuarios(a, b)              diferencias de ficha y de menú entre dos usuarios.

Escritura (única: alta de usuario en Usuarios.aspx; `dry_run=True` por defecto):
  crear_usuario(...)     da de alta un usuario con un rol existente. La contraseña temporal NO se
                         pasa por parámetro: la guarda el supervisor con `_stod_env.py setclave`.

No genera reportes, no cierra la sesión, no edita ni borra usuarios ni toca permisos de roles.
"""
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _stod_env  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
mcp = FastMCP("stod")
RO = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)

MENU_TTL = 1800          # segundos que se reutiliza el menú leído
_PATH_OK = re.compile(r"^[A-Za-z0-9_./%-]+\.(aspx|htm|html)(\?[A-Za-z0-9_=&.%-]*)?$", re.I)
_BLOQUEADAS = ("logout", "cerrarsesion")


def _decode(content: bytes) -> str:
    """El portal declara UTF-8 pero a veces sirve Latin-1: probar UTF-8 y caer a cp1252."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


# ---------------------------------------------------------------- análisis del HTML
class _Campos(HTMLParser):
    """Campos del formulario de login (incluye los ocultos de ASP.NET)."""

    def __init__(self):
        super().__init__()
        self.inputs: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("name"):
            self.inputs[a["name"]] = a.get("value") or ""


class _Menu(HTMLParser):
    """Árbol de menús (div.menuv > ul > li > a). Un enlace '#' con submenú es un grupo."""

    def __init__(self):
        super().__init__()
        self.items: list[dict] = []
        self._path: list[str] = []
        self._ul_push: list[bool] = []
        self._pending: str | None = None
        self._href: str | None = None
        self._label = ""
        self._in_a = False
        self._menu_depth = 0
        self._div_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div":
            self._div_depth += 1
            if "menuv" in (a.get("class") or "") and not self._menu_depth:
                self._menu_depth = self._div_depth
        if not self._menu_depth:
            return
        if tag == "ul":
            if self._pending is not None:
                self._path.append(self._pending)
                self._ul_push.append(True)
            else:
                self._ul_push.append(False)
            self._pending = None
        elif tag == "a":
            self._in_a, self._href, self._label = True, a.get("href") or "", ""

    def handle_endtag(self, tag):
        if tag == "div":
            if self._menu_depth == self._div_depth:
                self._menu_depth = 0
            self._div_depth -= 1
            return
        if not self._menu_depth:
            return
        if tag == "ul" and self._ul_push:
            if self._ul_push.pop():
                self._path.pop()
        elif tag == "a" and self._in_a:
            self._in_a = False
            label = re.sub(r"\s+", " ", self._label).strip()
            href = (self._href or "").strip()
            if not label or label.lower().startswith("omitir v"):
                return
            if href in ("", "#") or href.startswith("#"):
                self._pending = label            # grupo: el siguiente <ul> es su contenido
            else:
                self.items.append({
                    "ruta": " > ".join(self._path), "nombre": label, "href": href.replace("\\", "/"),
                    "tipo": "reporte" if "reporteid=" in href.lower() else "pagina",
                })

    def handle_data(self, d):
        if self._in_a:
            self._label += d


class _Texto(HTMLParser):
    """Texto visible de una página sin menú, scripts ni estilos; tablas como filas 'a | b'."""

    SKIP = {"script", "style", "noscript"}

    def __init__(self, solo_contenido: bool = True):
        super().__init__()
        self.solo_contenido = solo_contenido     # ignorar lo anterior a ContentPlaceHolder1 (cuadro lateral)
        self.encontro = not solo_contenido
        self.title = ""
        self.lines: list[str] = []
        self.campos: list[dict] = []
        self._skip = 0
        self._menu_depth = 0
        self._div_depth = 0
        self._nav = 0
        self._in_title = False
        self._buf: list[str] = []
        self._row: list[str] = []
        self._sel: dict | None = None

    @property
    def _oculto(self):
        return self._skip or self._menu_depth or self._nav or not self.encontro

    def _flush_line(self):
        t = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        if t:
            self.lines.append(t)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if not self.encontro and "ContentPlaceHolder1" in ((a.get("id") or "") + (a.get("name") or "")):
            self.encontro = True
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "nav":
            self._nav += 1
        elif tag == "div":
            self._div_depth += 1
            if "menuv" in (a.get("class") or "") and not self._menu_depth:
                self._menu_depth = self._div_depth
        if self._oculto:
            return
        if tag in ("br", "p", "li", "h1", "h2", "h3", "h4", "div"):
            self._flush_line()
        elif tag == "input":
            name, typ = a.get("name") or "", (a.get("type") or "text").lower()
            if name and typ not in ("hidden", "submit", "button", "image") and not name.startswith(("__", "Rad", "ctl00$txtCodigo")):
                self.campos.append({"campo": name, "tipo": typ, "valor": a.get("value") or ""})
            elif typ == "submit" and a.get("value") and not name.endswith("btnBuscar"):
                self.campos.append({"campo": name, "tipo": "boton", "valor": a.get("value")})
        elif tag == "select" and a.get("name"):
            self._sel = {"campo": a["name"], "tipo": "lista", "valor": "", "opciones": []}
        elif tag == "option" and self._sel is not None:
            self._sel["opciones"].append(a.get("value") or "")
            if "selected" in a:
                self._sel["valor"] = a.get("value") or ""
        elif tag == "textarea" and a.get("name"):
            self.campos.append({"campo": a["name"], "tipo": "texto largo", "valor": ""})

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "nav" and self._nav:
            self._nav -= 1
        elif tag == "div":
            if self._menu_depth == self._div_depth:
                self._menu_depth = 0
            self._div_depth -= 1
        if self._oculto:
            return
        if tag in ("td", "th"):
            t = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self._buf = []
            if t:
                self._row.append(t)
        elif tag == "tr":
            self._flush_line()
            if self._row:
                self.lines.append(" | ".join(self._row))
                self._row = []
        elif tag == "select" and self._sel is not None:
            self._sel["opciones"] = self._sel["opciones"][:25]
            self.campos.append(self._sel)
            self._sel = None
        elif tag in ("p", "li", "h1", "h2", "h3", "h4", "div"):
            self._flush_line()

    def handle_data(self, d):
        if self._in_title:
            self.title += d
        elif not self._oculto:
            self._buf.append(d)


# ---------------------------------------------------------------- sesión
class _Sesion:
    def __init__(self):
        self._lock = threading.Lock()
        self._client: httpx.Client | None = None
        self._base = ""
        self._menu: list[dict] = []
        self._menu_ts = 0.0

    def _login(self) -> None:
        base, user, pwd = _stod_env.credentials()
        # trust_env=False: stod.net es interno (10.0.0.180); no pasa por el proxy corporativo
        c = httpx.Client(follow_redirects=True, timeout=60, trust_env=False,
                         headers={"User-Agent": "Mozilla/5.0 (mcp-stod)"})
        r = c.get(f"{base}/Portal.aspx")
        f = _Campos()
        f.feed(_decode(r.content))
        datos = dict(f.inputs)
        datos.update({"txtUsuario": user, "txtPass": pwd, "Button1": "Validar"})
        r = c.post(f"{base}/Portal.aspx", data=datos)
        if ".ASPXAUTH" not in c.cookies:
            c.close()
            raise RuntimeError("STOD rechazó el usuario o la contraseña guardados "
                               f"(credencial 'stod/portal'). Revise con: python {Path(_stod_env.__file__)} set")
        self._client, self._base = c, base
        self._set_menu(_decode(r.content))

    def _set_menu(self, html: str) -> None:
        m = _Menu()
        m.feed(html)
        if m.items:
            self._menu, self._menu_ts = m.items, time.time()

    def get(self, path: str) -> httpx.Response:
        with self._lock:
            for intento in (1, 2):
                if self._client is None:
                    self._login()
                r = self._client.get(f"{self._base}/{path}")
                if 'id="txtPass"' in _decode(r.content[:200000]) and intento == 1:
                    self._client.close()          # la sesión caducó: reingresar una vez
                    self._client = None
                    continue
                return r
        raise RuntimeError("No se pudo mantener la sesión de STOD.")

    def post(self, path: str, data: dict) -> httpx.Response:
        with self._lock:
            if self._client is None:
                self._login()
            r = self._client.post(f"{self._base}/{path}", data=data)
            if 'id="txtPass"' in _decode(r.content[:200000]):
                self._client.close()
                self._client = None
                raise RuntimeError("La sesión de STOD caducó a media operación; repita la consulta.")
            return r

    def menu(self) -> list[dict]:
        with self._lock:
            vigente = self._menu and time.time() - self._menu_ts < MENU_TTL
        if not vigente:
            r = self.get("Default.aspx")
            with self._lock:
                self._set_menu(_decode(r.content))
        return self._menu


_S = _Sesion()


def _test() -> str:
    """Para `_stod_env.py status`: ingresa y cuenta las opciones del menú."""
    _S._login()
    return f"ingreso OK ({len(_S._menu)} opciones de menú)"


# ---------------------------------------------------------------- administración (Telerik RadGrid)
UID_USU = "ctl00$ContentPlaceHolder1$grdUsers"
UID_ROL = "ctl00$ContentPlaceHolder1$gridRoles"
SCAN_TTL = 86400         # 1 día (caché también en disco: %LOCALAPPDATA%\mcp-stod\cache.json)
_cache = {"usu": {"ts": 0.0, "filas": []}, "rol": {"ts": 0.0, "filas": []}}
_USER_OK = re.compile(r"^[A-Za-z0-9._-]{3,30}$")
_EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


class _Form(HTMLParser):
    """Todos los campos que enviaría el navegador (ocultos de ASP.NET incluidos), menos los botones."""

    def __init__(self):
        super().__init__()
        self.campos: dict[str, str] = {}
        self._sel: str | None = None
        self._sel_val: str | None = None
        self._sel_first: str | None = None
        self._ta: str | None = None
        self._ta_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        n = a.get("name")
        if tag == "input" and n:
            t = (a.get("type") or "text").lower()
            if t in ("submit", "button", "image", "file", "reset"):
                return
            if t in ("checkbox", "radio") and "checked" not in a:
                return
            self.campos[n] = a.get("value") or ("on" if t == "checkbox" else "")
        elif tag == "select" and n:
            self._sel, self._sel_val, self._sel_first = n, None, None
        elif tag == "option" and self._sel:
            v = a.get("value")
            v = v if v is not None else ""
            if self._sel_first is None:
                self._sel_first = v
            if "selected" in a:
                self._sel_val = v
        elif tag == "textarea" and n:
            self._ta, self._ta_buf = n, []

    def handle_endtag(self, tag):
        if tag == "select" and self._sel:
            self.campos[self._sel] = self._sel_val if self._sel_val is not None else (self._sel_first or "")
            self._sel = None
        elif tag == "textarea" and self._ta:
            self.campos[self._ta] = "".join(self._ta_buf)
            self._ta = None

    def handle_data(self, d):
        if self._ta:
            self._ta_buf.append(d)


def _plano(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _postback(path: str, html: str, target: str = "", arg: str = "", extra: dict | None = None) -> str:
    f = _Form()
    f.feed(html)
    datos = dict(f.campos)
    datos["__EVENTTARGET"], datos["__EVENTARGUMENT"] = target, arg
    if extra:
        datos.update(extra)
    r = _S.post(path, datos)
    out = _decode(r.content)
    if r.status_code >= 400:
        m = re.search(r"<title>(.*?)</title>", out, re.S)
        raise RuntimeError(f"STOD respondió {r.status_code}: {re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else out[:200]}")
    return out


class _Grid:
    """Un RadGrid de STOD navegado por postbacks (FireCommand de Telerik: PageSize y Page)."""

    def __init__(self, path: str, uid: str, tamano: int = 100):
        self.path, self.uid, self.t = path, uid, f"{uid}$ctl00"
        self.html = _decode(_S.get(path).content)
        self.pag = 1
        self._cmd(f"PageSize;{tamano}")

    def _cmd(self, cmd: str) -> None:
        self.html = _postback(self.path, self.html, target=self.uid, arg=f"FireCommand:{self.t};{cmd}")

    def paginas(self) -> int:
        m = re.search(r"Mostrando Pag\.? (\d+) de (\d+)", _plano(self.html))
        return int(m.group(2)) if m else 1

    def ir(self, n: int) -> None:
        if n != self.pag:
            self._cmd(f"Page;{n}")
            self.pag = n

    def filas(self) -> list[dict]:
        out = []
        for m in re.finditer(r'<tr class="rg(?:Row|AltRow)"[^>]*>(.*?)</tr>', self.html, re.S):
            row = m.group(1)
            celdas = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip()
                      for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            eb = re.search(r'name="([^"]*\$EditButton)"', row)
            pm = re.search(r"__doPostBack\(&#39;([^&]*lnkPermisos)&#39;", row)
            out.append({"celdas": celdas, "edit": eb.group(1) if eb else "", "permisos": pm.group(1) if pm else ""})
        return out

    def fila(self, valor: str) -> dict | None:
        for f in self.filas():
            if f["celdas"] and f["celdas"][0].lower() == valor.lower():
                return f
        return None


def _disco() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "mcp-stod" / "cache.json"


def _leer_disco(clave: str) -> dict | None:
    try:
        d = json.loads(_disco().read_text(encoding="utf-8")).get(clave)
        return d if d and d.get("filas") else None
    except Exception:
        return None


def _guardar_disco(clave: str, ts: float, filas: list[dict]) -> None:
    try:
        p = _disco()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            todo = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            todo = {}
        todo[clave] = {"ts": ts, "filas": filas}
        p.write_text(json.dumps(todo, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass            # el caché es solo una ayuda: si no se puede escribir, se sigue sin él


def _invalidar(clave: str) -> None:
    _cache[clave]["ts"] = 0.0
    _cache[clave]["filas"] = []
    try:
        p = _disco()
        todo = json.loads(p.read_text(encoding="utf-8"))
        todo.pop(clave, None)
        p.write_text(json.dumps(todo, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _escanear(clave: str, refrescar: bool = False) -> list[dict]:
    """Recorre todo el grid (usuarios o roles) y guarda celdas y página. Caché en memoria y en disco
    (SCAN_TTL = 1 día): la primera lectura del día tarda ~50 s; refrescar=True la fuerza."""
    c = _cache[clave]
    if not refrescar:
        if c["filas"] and time.time() - c["ts"] < SCAN_TTL:
            return c["filas"]
        d = _leer_disco(clave)
        if d and time.time() - d["ts"] < SCAN_TTL:
            c["ts"], c["filas"] = d["ts"], d["filas"]
            return c["filas"]
    path, uid = ("Usuarios.aspx", UID_USU) if clave == "usu" else ("Roles.aspx", UID_ROL)
    g = _Grid(path, uid)
    filas = []
    for p in range(1, g.paginas() + 1):
        g.ir(p)
        for f in g.filas():
            filas.append({"celdas": f["celdas"], "pagina": p})
    c["ts"], c["filas"] = time.time(), filas
    _guardar_disco(clave, c["ts"], filas)
    return filas


def _form_usuario(html: str, sufijo: str) -> tuple[str, dict]:
    """(prefijo del renglón de edición/alta, campos). sufijo: 'cmbRolesEdit' o 'cmbRolesInsert'."""
    f = _Form()
    f.feed(html)
    for k in f.campos:
        if k.endswith("$" + sufijo):
            return k[: -len(sufijo)], f.campos
    raise RuntimeError("STOD no abrió el formulario del usuario (¿cambió la página?).")


def _ficha(campos: dict, p: str) -> dict:
    return {
        "usuario": campos.get(p + "RadTextBox22", ""), "depto": campos.get(p + "txtDeptoEdit", ""),
        "cod_vendedor": campos.get(p + "txtCodVendedor", ""), "nombre": campos.get(p + "RadTextBox1", ""),
        "apellido": campos.get(p + "RadTextBox21", ""), "email": campos.get(p + "RadTextBox44", ""),
        "es_gerente": (p + "ctl04") in campos, "crea_tickets_terceros": (p + "ctl05") in campos,
        "usuario_sap": campos.get(p + "TB_UsuarioSAP", ""), "nit": campos.get(p + "TB_NIT", ""),
        "rol": campos.get(p + "cmbRolesEdit", ""),
    }


def _ficha_usuario(usuario: str) -> dict | None:
    """Abre el formulario de edición del usuario (no guarda) y devuelve su ficha; None si no existe."""
    for refrescar in (False, True):          # con el caché del día la página puede haber cambiado: releer una vez
        if refrescar and time.time() - _cache["usu"]["ts"] < 600:
            continue                         # lectura reciente (<10 min): no repetir el recorrido de ~50 s
        fila = next((f for f in _escanear("usu", refrescar) if f["celdas"] and f["celdas"][0].lower() == usuario.lower()), None)
        if not fila:
            continue
        g = _Grid("Usuarios.aspx", UID_USU)
        g.ir(fila["pagina"])
        f = g.fila(usuario)
        if f and f["edit"]:
            break
    else:
        return None
    html = _postback("Usuarios.aspx", g.html, extra={f["edit"] + ".x": "5", f["edit"] + ".y": "5"})
    p, campos = _form_usuario(html, "cmbRolesEdit")
    return _ficha(campos, p)


class _Arbol(HTMLParser):
    """Nodos de un RadTreeView (trvMenusPorRole): devuelve las hojas con su ruta de grupos."""

    def __init__(self, ident: str):
        super().__init__()
        self.ident, self.nodos = ident, []
        self._on, self._div, self._ul = False, 0, 0
        self._in, self._href, self._txt = False, "", ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div":
            if self._on:
                self._div += 1
            elif self.ident in (a.get("id") or ""):
                self._on, self._div = True, 1
        if not self._on:
            return
        if tag == "ul":
            self._ul += 1
        elif tag in ("a", "span") and "rtIn" in (a.get("class") or "").split():
            self._in, self._href, self._txt = tag, (a.get("href") or "").replace("\\", "/"), ""

    def handle_endtag(self, tag):
        if not self._on:
            return
        if tag == self._in:
            self.nodos.append({"nivel": self._ul, "nombre": re.sub(r"\s+", " ", self._txt).strip(), "href": self._href})
            self._in = False
        elif tag == "ul":
            self._ul -= 1
        elif tag == "div":
            self._div -= 1
            if self._div == 0:
                self._on = False

    def handle_data(self, d):
        if self._in:
            self._txt += d


def _hojas(nodos: list[dict]) -> tuple[list[dict], int]:
    ultimo: dict[int, str] = {}
    hojas, grupos = [], 0
    for i, n in enumerate(nodos):
        ultimo[n["nivel"]] = n["nombre"]
        sig = nodos[i + 1]["nivel"] if i + 1 < len(nodos) else 0
        if sig > n["nivel"]:
            grupos += 1
            continue
        ruta = " > ".join(ultimo[k] for k in sorted(ultimo) if k < n["nivel"])
        hojas.append({"ruta": ruta, "nombre": n["nombre"], "href": n["href"]})
    return hojas, grupos


# ---------------------------------------------------------------- herramientas
@mcp.tool(annotations=RO)
def menu(texto: str = "", limite: int = 40) -> dict:
    """Busca en el menú de STOD del usuario (opciones y reportes).
    texto: palabras a buscar en la ruta y el nombre (sin distinguir mayúsculas ni tildes; todas deben
    aparecer). Vacío = solo cuenta y lista las secciones principales. Cada resultado trae `href`,
    que se puede leer con `page`. tipo 'reporte' = VisorReportes.aspx?ReporteID=N."""
    items = _S.menu()
    palabras = _norm(texto).split()
    if not palabras:
        secciones: dict[str, int] = {}
        for i in items:
            s = i["ruta"].split(" > ")[0] or "(sin sección)"
            secciones[s] = secciones.get(s, 0) + 1
        return {"total": len(items), "secciones": dict(sorted(secciones.items(), key=lambda kv: -kv[1]))}
    hit = [i for i in items if all(p in _norm(i["ruta"] + " " + i["nombre"]) for p in palabras)]
    return {"total": len(items), "coinciden": len(hit), "mostrados": min(len(hit), limite), "resultados": hit[:limite]}


@mcp.tool(annotations=RO)
def page(path: str, max_chars: int = 8000) -> dict:
    """Lee una página de STOD con GET y devuelve su título, su texto (sin el menú) y sus campos de
    formulario. path es relativo a /stodnet, p. ej. 'INFO_Extensiones.aspx' o
    'VisorReportes.aspx?ReporteID=2068' (los que devuelve `menu`). No envía formularios ni genera
    reportes. Si el servidor responde con error, devuelve el mensaje (sin la traza)."""
    path = path.strip().replace("\\", "/")
    if (not _PATH_OK.match(path) or path.startswith("/") or ".." in path
            or any(b in path.lower() for b in _BLOQUEADAS)):
        raise ValueError("path no válido: use una página relativa (.aspx/.htm) como las que da `menu`.")
    r = _S.get(path)
    html = _decode(r.content)
    t = _Texto()
    t.feed(html)
    if not t.encontro or not t.lines:          # página sin ContentPlaceHolder1 (.htm, errores): todo el texto
        t = _Texto(solo_contenido=False)
        t.feed(html)
    titulo = re.sub(r"\s+", " ", t.title).strip()
    if r.status_code >= 400:
        return {"path": path, "status": r.status_code, "error": titulo or html[:300]}
    texto = "\n".join(t.lines)
    return {"path": path, "status": r.status_code, "titulo": titulo, "campos": t.campos[:60],
            "texto": texto[:max_chars], "truncado": len(texto) > max_chars, "caracteres": len(texto)}


@mcp.tool(annotations=RO)
def usuarios(texto: str, limite: int = 30, refrescar: bool = False) -> dict:
    """Busca usuarios de STOD (Administración del Sistema > Usuarios) por usuario, nombre o apellido
    (sin tildes, en cualquier orden). Recorre el listado completo (~2,500; la primera lectura del día
    tarda ~50 s; luego se reutiliza desde un caché en disco de 1 día; refrescar=True lo vuelve a leer).
    Devuelve usuario, nombre y apellido."""
    palabras = _norm(texto).split()
    if not palabras:
        raise ValueError("Indique texto a buscar.")
    filas = _escanear("usu", refrescar)
    hit = [{"usuario": f["celdas"][0], "nombre": f["celdas"][1], "apellido": f["celdas"][2]}
           for f in filas if len(f["celdas"]) >= 3
           and all(p in _norm(" ".join(f["celdas"][:3])) for p in palabras)]
    return {"total_usuarios": len(filas), "coinciden": len(hit), "resultados": hit[:limite]}


@mcp.tool(annotations=RO)
def usuario(usuario: str) -> dict:
    """Ficha de un usuario de STOD: depto, cod. vendedor, nombre, apellido, correo, es_gerente,
    crea_tickets_terceros, usuario SAP, NIT y rol. Abre su formulario de edición sin guardar nada;
    jamás devuelve la contraseña."""
    ficha = _ficha_usuario(usuario.strip())
    if not ficha:
        return {"existe": False, "usuario": usuario}
    return {"existe": True, **ficha}


@mcp.tool(annotations=RO)
def roles(texto: str = "") -> dict:
    """Roles de STOD (Administración > Roles). Un usuario tiene UN rol y el menú que ve sale de él.
    texto filtra por nombre (vacío = cantidad y primeros 100)."""
    filas = [f["celdas"][0] for f in _escanear("rol") if f["celdas"]]
    palabras = _norm(texto).split()
    hit = [r for r in filas if all(p in _norm(r) for p in palabras)]
    return {"total": len(filas), "coinciden": len(hit), "roles": hit[:100]}


@mcp.tool(annotations=RO)
def permisos_rol(rol: str, max_items: int = 400) -> dict:
    """Menús que da un rol de STOD (los que aparecen en 'Permisos' del rol): ruta, nombre y href.
    Solo lectura; no usa el botón Asignar."""
    fila = next((f for f in _escanear("rol") if f["celdas"] and f["celdas"][0].lower() == rol.strip().lower()), None)
    if not fila:
        return {"existe": False, "rol": rol}
    g = _Grid("Roles.aspx", UID_ROL)
    g.ir(fila["pagina"])
    f = g.fila(rol.strip())
    if not f or not f["permisos"]:
        return {"existe": False, "rol": rol}
    html = _postback("Roles.aspx", g.html, target=f["permisos"])
    a = _Arbol("trvMenusPorRole")
    a.feed(html)
    hojas, grupos = _hojas(a.nodos)
    return {"existe": True, "rol": f["celdas"][0], "grupos": grupos, "opciones": len(hojas),
            "mostradas": min(len(hojas), max_items), "menus": hojas[:max_items]}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _norm(s))


@mcp.tool(annotations=RO)
def proponer_usuario(nombre: str, apellido: str) -> dict:
    """Propone nombres de usuario libres para STOD con la convención de Acme: inicial del primer
    nombre + primer apellido (mlima, fpozuelos, user1). Si está tomado, prueba con el segundo
    apellido, con más letras del nombre y con el segundo nombre. Devuelve candidatos libres/tomados y
    los usuarios que ya existen con el mismo apellido y primer nombre (posible duplicado de persona)."""
    nom, ape = _norm(nombre).split(), _norm(apellido).split()
    ape = [a for a in ape if a not in ("de", "del", "la", "las", "los", "y")] or ape
    if not nom or not ape:
        raise ValueError("Indique nombre y apellido.")
    n1, a1 = _slug(nom[0]), _slug(ape[0])
    cand = [n1[0] + a1]
    if len(ape) > 1:
        cand.append(n1[0] + _slug(ape[1]))
    if len(nom) > 1:
        cand.append(n1[0] + _slug(nom[1])[0] + a1)
    cand += [n1[:2] + a1, n1[:3] + a1]
    cand = list(dict.fromkeys(c for c in cand if 3 <= len(c) <= 30))
    filas = _escanear("usu")
    en_uso = {f["celdas"][0].lower() for f in filas if f["celdas"]}
    homonimos = [{"usuario": f["celdas"][0], "nombre": f["celdas"][1], "apellido": f["celdas"][2]}
                 for f in filas if len(f["celdas"]) >= 3
                 and _norm(f["celdas"][2]).split()[:1] == ape[:1] and _norm(f["celdas"][1]).split()[:1] == nom[:1]]
    libres = [c for c in cand if c not in en_uso]
    return {"recomendado": libres[0] if libres else None, "libres": libres,
            "tomados": [c for c in cand if c in en_uso], "posibles_duplicados_de_persona": homonimos}


@mcp.tool(annotations=RO)
def comparar_usuarios(a: str, b: str) -> dict:
    """Compara dos usuarios de STOD: ficha (depto, rol, correo…) y diferencias de menú entre sus roles.
    Útil para «mismos accesos que X»: si el rol es el mismo, no hay diferencias de permisos."""
    fa, fb = _ficha_usuario(a.strip()), _ficha_usuario(b.strip())
    if not fa or not fb:
        return {"existe_a": bool(fa), "existe_b": bool(fb)}
    campos = ("depto", "rol", "email", "cod_vendedor", "es_gerente", "crea_tickets_terceros", "usuario_sap")
    dif = {k: {a: fa[k], b: fb[k]} for k in campos if fa[k] != fb[k]}
    out = {"usuario_a": fa, "usuario_b": fb, "diferencias_de_ficha": dif}
    if fa["rol"] == fb["rol"]:
        out["menus"] = f"mismo rol ({fa['rol']}): mismos permisos"
        return out
    ma, mb = (permisos_rol(fa["rol"], 5000), permisos_rol(fb["rol"], 5000))
    ka = {(m["ruta"], m["nombre"]) for m in ma.get("menus", [])}
    kb = {(m["ruta"], m["nombre"]) for m in mb.get("menus", [])}
    out["menus"] = {"solo_en_a": sorted(" > ".join(x) for x in ka - kb)[:200],
                    "solo_en_b": sorted(" > ".join(x) for x in kb - ka)[:200], "en_comun": len(ka & kb)}
    return out


def _clave_ok(c: str) -> bool:
    """Formato que exige STOD: 8+ caracteres con mayúscula, dígito y uno de @#$%^&+= (validador de la página)."""
    return _stod_env.clave_valida(c)


@mcp.tool(annotations=WRITE)
def crear_usuario(usuario: str, nombre: str, apellido: str, email: str, rol: str = "", depto: str = "",
                  nit: str = "", cod_vendedor: str = "", es_gerente: bool | None = None,
                  crea_tickets_terceros: bool | None = None, usuario_sap: str = "",
                  copiar_de: str = "", dry_run: bool = True) -> dict:
    """Da de alta un usuario en STOD (Administración > Usuarios > Agregar Usuario).
    El rol debe existir (ver `roles`); copiar_de='otrousuario' toma de él rol, depto, cod. vendedor y
    banderas (lo que se indique explícito tiene prioridad). La contraseña NO es parámetro: se toma de la
    credencial 'stod/clave_inicial' que el supervisor genera con `python _stod_env.py nuevaclave` (aleatoria, la
    copia al portapapeles sin mostrarla) o guarda con `setclave`, y nunca se
    devuelve. dry_run=True (por defecto) solo valida y muestra qué se enviaría; con dry_run=False crea el
    usuario y lo verifica releyéndolo. Confirmar con el supervisor antes de dry_run=False."""
    usuario, nombre, apellido, email = usuario.strip(), nombre.strip(), apellido.strip(), email.strip()
    if not _USER_OK.match(usuario):
        raise ValueError("usuario: 3-30 caracteres (letras, números, . _ -), sin espacios.")
    if not nombre or not apellido or not _EMAIL_OK.match(email):
        raise ValueError("Faltan nombre, apellido o el correo no es válido.")
    if not dry_run:
        _escanear("usu", refrescar=True)      # al crear de verdad, comprobar contra la lista actual, no el caché
    if _ficha_usuario(usuario):
        return {"creado": False, "error": f"El usuario «{usuario}» ya existe en STOD."}
    base = {}
    if copiar_de.strip():
        base = _ficha_usuario(copiar_de.strip())
        if not base:
            raise ValueError(f"copiar_de: no existe el usuario «{copiar_de}».")
    plan = {
        "usuario": usuario, "nombre": nombre, "apellido": apellido, "email": email,
        "rol": rol.strip() or base.get("rol", ""), "depto": depto.strip() or base.get("depto", ""),
        "cod_vendedor": cod_vendedor.strip() or base.get("cod_vendedor", "") or "0",
        "es_gerente": base.get("es_gerente", False) if es_gerente is None else es_gerente,
        "crea_tickets_terceros": base.get("crea_tickets_terceros", False) if crea_tickets_terceros is None else crea_tickets_terceros,
        "usuario_sap": usuario_sap.strip() or base.get("usuario_sap", ""), "nit": nit.strip(),
    }
    if not plan["rol"]:
        raise ValueError("Indique rol o copiar_de.")
    roles_ok = [f["celdas"][0] for f in _escanear("rol") if f["celdas"]]
    if plan["rol"] not in roles_ok:
        raise ValueError(f"El rol «{plan['rol']}» no existe en STOD. Use la herramienta `roles`.")
    homonimos = [f["celdas"][0] for f in _escanear("usu")
                 if len(f["celdas"]) >= 3 and _norm(f["celdas"][2]) == _norm(apellido)
                 and _norm(f["celdas"][1]).split()[:1] == _norm(nombre).split()[:1]]
    clave = _stod_env.clave_inicial()
    estado = {"clave_inicial_guardada": bool(clave), "clave_cumple_formato": _clave_ok(clave) if clave else False}
    if dry_run:
        return {"dry_run": True, "se_enviaria": plan, "posibles_duplicados_por_nombre": homonimos, **estado,
                "nota": "Falta guardar la contraseña temporal: python _stod_env.py setclave" if not clave else
                        ("La contraseña guardada no cumple el formato (8+, mayúscula, dígito, especial)."
                         if not estado["clave_cumple_formato"] else "Listo para crear con dry_run=False.")}
    if not clave or not estado["clave_cumple_formato"]:
        raise RuntimeError("No hay contraseña temporal válida guardada: python _stod_env.py setclave")
    html = _decode(_S.get("Usuarios.aspx").content)
    T = f"{UID_USU}$ctl00"
    html = _postback("Usuarios.aspx", html, target=f"{T}$ctl02$ctl00$InitInsertButton")
    p, campos = _form_usuario(html, "cmbRolesInsert")
    valores = {"RadTextBox22": usuario, "txtDeptoEdit": plan["depto"], "txtPassword": clave,
               "txtPasswordConfirmation": clave, "txtCodVendedor": plan["cod_vendedor"],
               "RadTextBox1": nombre, "RadTextBox21": apellido, "RadTextBox44": email,
               "TB_UsuarioSAP": plan["usuario_sap"], "TB_NIT": plan["nit"], "cmbRolesInsert": plan["rol"]}
    for k, v in valores.items():
        campos[p + k] = v
        # RadTextBox de Telerik guarda su valor también en un oculto cuyo nombre usa '_' (id), no '$'
        cs = next((c for c in campos if c.endswith("_" + k + "_ClientState")), "")
        if cs:
            try:
                import json
                d = json.loads(campos[cs] or "{}")
                d.setdefault("enabled", True)
                d.setdefault("emptyMessage", "")
                d["validationText"] = d["valueAsString"] = d["lastSetTextBoxValue"] = v
                campos[cs] = json.dumps(d, separators=(",", ":"))
            except Exception:
                pass
    for k, marcado in (("ctl04", plan["es_gerente"]), ("ctl05", plan["crea_tickets_terceros"])):
        if marcado:
            campos[p + k] = "on"
        else:
            campos.pop(p + k, None)
    campos["__EVENTTARGET"], campos["__EVENTARGUMENT"] = p + "PerformInsertButton", ""
    r = _S.post("Usuarios.aspx", campos)
    resp = _decode(r.content)
    _invalidar("usu")
    sigue = (p + "cmbRolesInsert") in resp
    ficha = _ficha_usuario(usuario)
    return {"creado": bool(ficha), "http": r.status_code, "formulario_sigue_abierto": sigue,
            "usuario_en_stod": ficha, "contrasena": "la temporal guardada en stod/clave_inicial (no se muestra)"}


# ---------------------------------------------------------------- reportes, diagnóstico y clave
def _rad_set(campos: dict, nombre: str, valor: str) -> None:
    """Pone el valor de un RadTextBox y de su oculto *_ClientState (nombre con '_' en vez de '$')."""
    campos[nombre] = valor
    cs = nombre.replace("$", "_") + "_ClientState"
    if cs in campos:
        try:
            d = json.loads(campos[cs] or "{}")
        except Exception:
            d = {}
        d.setdefault("enabled", True)
        d.setdefault("emptyMessage", "")
        d["validationText"] = d["valueAsString"] = d["lastSetTextBoxValue"] = valor
        campos[cs] = json.dumps(d, separators=(",", ":"))


def _campo_por_sufijo(campos: dict, nombre: str) -> str:
    """Nombre completo de un campo a partir del nombre exacto o de su último segmento (tras '$')."""
    if nombre in campos:
        return nombre
    hit = [k for k in campos if k.split("$")[-1].lower() == nombre.lower() and not k.startswith("__")]
    if len(hit) != 1:
        raise ValueError(f"Campo «{nombre}»: {'no existe' if not hit else 'ambiguo ' + str(hit)}. "
                         f"Campos: {[k.split('$')[-1] for k in campos if 'ContentPlaceHolder1' in k]}")
    return hit[0]


_REPORTE_OK = re.compile(r"^\d{1,6}$")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
def ejecutar_reporte(reporte_id: int, parametros: dict | None = None, exportar: bool = False,
                     guardar_en: str = "") -> dict:
    """Reporte de STOD (VisorReportes.aspx?ReporteID=N, los que da `menu` con tipo 'reporte').
    exportar=False (por defecto) solo lista el procedimiento y los controles de parámetros del reporte.
    exportar=True llena `parametros` ({campo: valor}; el campo puede ser el último segmento del nombre)
    y pulsa «Exporta a Excel»: si el servidor devuelve un archivo lo guarda (guardar_en, o
    %LOCALAPPDATA%\\mcp-stod\\reportes) y devuelve ruta, tamaño y las primeras filas; si devuelve HTML,
    devuelve su texto y los mensajes de error. Un reporte solo consulta (procedimiento almacenado);
    no modifica datos."""
    if not _REPORTE_OK.match(str(reporte_id)):
        raise ValueError("reporte_id no válido.")
    path = f"VisorReportes.aspx?ReporteID={int(reporte_id)}"
    html = _decode(_S.get(path).content)
    f = _Form()
    f.feed(html)
    titulo = re.search(r'id="ContentPlaceHolder1_lblHeader"[^>]*>([^<]*)<', html)
    sp = f.campos.get("ctl00$ContentPlaceHolder1$hiddenfilSP", "")
    tabla = re.search(r'<table id="ContentPlaceHolder1_tblcontrols".*?</table>', html, re.S)
    controles = {k: v for k, v in f.campos.items()
                 if tabla and k.split("$")[-1] in (tabla.group(0) or "") and "hidden" not in k.split("$")[-1].lower()}
    info = {"reporte_id": reporte_id, "titulo": (titulo.group(1).strip() if titulo else ""), "procedimiento": sp,
            "controles_de_parametros": {k.split("$")[-1]: v for k, v in controles.items()},
            "texto_de_parametros": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", tabla.group(0))).strip() if tabla else ""}
    if not exportar:
        info["nota"] = ("Sin controles de parámetros: se puede exportar directo con exportar=True."
                        if not controles and not info["texto_de_parametros"] else "Pase los valores en `parametros`.")
        return info
    datos = dict(f.campos)
    for k, v in (parametros or {}).items():
        datos[_campo_por_sufijo(datos, k)] = str(v)
    datos["__EVENTTARGET"], datos["__EVENTARGUMENT"] = "", ""
    datos["ctl00$ContentPlaceHolder1$btnExportar"] = "Exporta a Excel"
    r = _S.post(path, datos)
    tipo = r.headers.get("content-type", "")
    disp = r.headers.get("content-disposition", "")
    info.update({"http": r.status_code, "content_type": tipo})
    if r.status_code < 400 and (disp or "html" not in tipo.lower()):
        nombre = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disp)
        ext = Path(nombre.group(1)).suffix if nombre else (".xlsx" if "spreadsheetml" in tipo else ".xls" if "excel" in tipo else ".bin")
        base = Path(guardar_en) if guardar_en else Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "mcp-stod" / "reportes"
        base.mkdir(parents=True, exist_ok=True)
        destino = base / f"reporte_{reporte_id}_{time.strftime('%Y%m%d_%H%M%S')}{ext}"
        destino.write_bytes(r.content)
        info.update({"archivo": str(destino), "bytes": len(r.content)})
        try:
            from openpyxl import load_workbook
            wb = load_workbook(destino, read_only=True, data_only=True)
            ws = wb.worksheets[0]
            filas = [[("" if c is None else str(c))[:40] for c in row] for _, row in zip(range(6), ws.iter_rows(values_only=True))]
            info["vista_previa"] = {"hoja": ws.title, "filas_de_ejemplo": filas}
        except Exception as e:                    # .xls HTML o formato no soportado: sin vista previa
            info["vista_previa"] = f"sin vista previa ({type(e).__name__})"
        return info
    out = _decode(r.content)
    t = _Texto()
    t.feed(out)
    info["texto"] = "\n".join(t.lines)[:2000]
    info["mensajes_de_error"] = _mensajes_error(out, t.lines)
    return info


def _mensajes_error(html: str, lineas: list[str]) -> list[str]:
    msgs = [l for l in lineas if re.search(r"\bERROR\b|no existe|no existen|inv[aá]lid", l, re.I)][:5]
    exc = re.search(r"((?:System|STOD_\w+)(?:\.\w+)*Exception)[^\n<]{0,200}", html)
    if exc:
        msgs.append(re.sub(r"\s+", " ", exc.group(0)).strip())
        msgs += [re.sub(r"\s+", " ", x)[:200] for x in re.findall(r"\n\s*(en [^\n<]{5,200}línea \d+)", html)[:3]]
    return msgs


# páginas de CONSULTA que `diagnosticar` puede probar, y los controles que se permiten en cada una.
_DIAG = {
    "feicpi_imprimirformulario.aspx": {"botones": {"Button1"}, "ui": set()},
    "feicpj_imprimirformulario.aspx": {"botones": {"Button1"}, "ui": set()},
    "enco_encomiendas.aspx": {"botones": {"BuscarBtn"}, "ui": set()},
    # pasos de pantalla de formularios que podrían registrar datos: solo con permitir_pasos_de_ui=True
    "enco_control.aspx": {"botones": set(), "ui": {"NuevaSolicitudBt"}},
    "feicpj_crearformulario.aspx": {"botones": set(), "ui": {"BtnIngresar"}},
}
_DIAG_PROHIBIDO = re.compile(r"guardar|grabar|insert|update|delete|elimin|modificar|actualiz|cerrar|anular|borrar", re.I)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
def diagnosticar(path: str, campos: dict | None = None, boton: str = "",
                 permitir_pasos_de_ui: bool = False, max_chars: int = 2500) -> dict:
    """Reproduce una falla de STOD sin guardar nada: carga una página de consulta, llena `campos`
    ({campo: valor}, nombre completo o último segmento), pulsa `boton` y devuelve el texto, los mensajes
    de error visibles y, si el servidor responde 500, el tipo de excepción y los primeros métodos de la pila.
    Solo páginas de la lista blanca (imprimir FEIC individual/jurídico, Consulta de Encomiendas).
    ENCO_Control y FEICPJ_CrearFormulario necesitan permitir_pasos_de_ui=True (botones «Nueva» /
    «Ingresar Formulario», que abren la pantalla de captura): confirmar con el supervisor antes de usarlo.
    Se rechazan botones de guardar, modificar, eliminar, cerrar o actualizar."""
    p = path.strip().replace("\\", "/")
    regla = _DIAG.get(p.lower())
    if not regla or not _PATH_OK.match(p):
        raise ValueError(f"Página no permitida. Lista blanca: {sorted(_DIAG)}")
    html = _decode(_S.get(p).content)
    f = _Form()
    f.feed(html)
    if not campos and not boton:
        t = _Texto()
        t.feed(html)
        return {"path": p, "campos": [{"campo": k.split("$")[-1], "valor": v} for k, v in f.campos.items()
                                      if "ContentPlaceHolder1" in k and "hidden" not in k.lower()],
                "botones": re.findall(r'<input[^>]*type="submit"[^>]*name="ctl00\$ContentPlaceHolder1\$([^"]+)"', html),
                "texto": "\n".join(t.lines)[:max_chars]}
    datos = dict(f.campos)
    for k, v in (campos or {}).items():
        datos[_campo_por_sufijo(datos, k)] = str(v)
    if boton:
        nombre = boton.split("$")[-1]
        permitidos = set(regla["botones"]) | (set(regla["ui"]) if permitir_pasos_de_ui else set())
        if _DIAG_PROHIBIDO.search(nombre) or nombre not in permitidos:
            raise ValueError(f"Botón «{nombre}» no permitido en {p}. Permitidos: {sorted(permitidos) or 'ninguno'}"
                             + ("" if permitir_pasos_de_ui or not regla["ui"] else " (los de pantalla requieren permitir_pasos_de_ui=True)"))
        m = re.search(r'name="ctl00\$ContentPlaceHolder1\$' + re.escape(nombre) + r'"[^>]*value="([^"]*)"', html)
        datos["ctl00$ContentPlaceHolder1$" + nombre] = m.group(1) if m else nombre
    datos["__EVENTTARGET"], datos["__EVENTARGUMENT"] = "", ""
    r = _S.post(p, datos)
    out = _decode(r.content)
    t = _Texto()
    t.feed(out)
    res = {"path": p, "http": r.status_code, "mensajes_de_error": _mensajes_error(out, t.lines)}
    pantalla = {
        "resultado": (re.search(r'id="ContentPlaceHolder1_ValidationCode"[^>]*>([^<]*)<', out) or [None, ""])[1].strip(),
        "mensaje_sistema": (re.search(r'id="lblMensajeSistema"[^>]*>([^<]*)<', out) or [None, ""])[1].strip(),
        "visor_pdf": (re.search(r'<iframe id="iFrameViewer" src="([^"]+)"', out) or [None, ""])[1],
    }
    res["pantalla"] = {k: v for k, v in pantalla.items() if v and v != "Comando:"}
    if r.status_code >= 400:
        m = re.search(r"<title>(.*?)</title>", out, re.S)
        res["titulo_error"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else ""
    res["texto"] = "\n".join(t.lines)[:max_chars]
    return res


def _target_clave(html: str, usuario: str) -> str:
    """Control «Camb. Pass» (lnkChangePassword) del renglón del usuario, o ''."""
    for m in re.finditer(r'<tr class="rg(?:Row|AltRow)"[^>]*>(.*?)</tr>', html, re.S):
        row = m.group(1)
        u = re.search(r'_lblUserName">([^<]*)<', row)
        if u and u.group(1).strip().lower() == usuario.lower():
            t = re.search(r"PostBackOptions\(&quot;([^&]*lnkChangePassword)&quot;", row)
            return t.group(1) if t else ""
    return ""


@mcp.tool(annotations=WRITE)
def restablecer_clave(usuario: str, dry_run: bool = True) -> dict:
    """Restablece la contraseña de un usuario de STOD con la temporal guardada en la credencial
    'stod/clave_inicial' (la genera el supervisor con `_stod_env.py nuevaclave`; NO es parámetro y nunca se
    devuelve). Usa «Camb. Pass» del listado de Usuarios (el mismo panel que el portal): abre el panel,
    llena Password nuevo y Confirmación y pulsa Guardar. dry_run=True (por defecto) solo abre el panel y
    verifica que el formulario y la clave están listos; con dry_run=False cambia la clave y comprueba que
    la ficha del usuario (rol, depto, correo, NIT…) no cambió. Confirmar con el supervisor antes de dry_run=False.
    El usuario debe cambiarla en su primer ingreso (Opciones de Usuario > Cambiar contraseña)."""
    usuario = usuario.strip()
    if not _USER_OK.match(usuario):
        raise ValueError("usuario: 3-30 caracteres (letras, números, . _ -), sin espacios.")
    antes = _ficha_usuario(usuario)
    if not antes:
        return {"cambiada": False, "error": f"El usuario «{usuario}» no existe en STOD."}
    clave = _stod_env.clave_inicial()
    estado = {"clave_inicial_guardada": bool(clave), "clave_cumple_formato": _clave_ok(clave) if clave else False}
    if not estado["clave_inicial_guardada"] or not estado["clave_cumple_formato"]:
        return {"cambiada": False, "dry_run": dry_run, **estado,
                "nota": "Falta una contraseña temporal válida: python _stod_env.py nuevaclave (o setclave)."}
    fila = next(x for x in _escanear("usu") if x["celdas"] and x["celdas"][0].lower() == usuario.lower())
    g = _Grid("Usuarios.aspx", UID_USU)
    g.ir(fila["pagina"])
    target = _target_clave(g.html, usuario)
    if not target:
        return {"cambiada": False, "error": "No se encontró «Camb. Pass» del usuario (¿caché desactualizado?): "
                                            "use usuarios(..., refrescar=True)."}
    panel = _postback("Usuarios.aspx", g.html, target=target)
    pf = _Form()
    pf.feed(panel)
    n_pass, n_conf = "ctl00$ContentPlaceHolder1$txtPassword", "ctl00$ContentPlaceHolder1$txtPasswordConfirmation"
    if n_pass not in pf.campos or "ContentPlaceHolder1_lnkGuardar" not in panel:
        raise RuntimeError("STOD no abrió el panel «Cambiar Password» (¿cambió la página?).")
    en_panel = re.search(r'id="ContentPlaceHolder1_lblUserName"[^>]*>([^<]*)<', panel)
    if not en_panel or en_panel.group(1).strip().lower() != usuario.lower():
        raise RuntimeError(f"El panel abierto es de «{en_panel.group(1) if en_panel else '?'}», no de {usuario}: no se cambió nada.")
    if dry_run:
        return {"dry_run": True, "usuario": usuario, "ficha": antes, **estado,
                "se_enviaria": "Password nuevo = Confirmación = clave temporal guardada (no se muestra); Guardar",
                "nota": "Listo para restablecer con dry_run=False."}
    datos = dict(pf.campos)
    _rad_set(datos, n_pass, clave)
    _rad_set(datos, n_conf, clave)
    datos["__EVENTTARGET"], datos["__EVENTARGUMENT"] = "ctl00$ContentPlaceHolder1$lnkGuardar", ""
    r = _S.post("Usuarios.aspx", datos)
    resp = _decode(r.content)
    sigue = "Cambiar Password" in _plano(resp)
    despues = _ficha_usuario(usuario)
    return {"cambiada": (r.status_code < 400 and not sigue), "http": r.status_code, "panel_sigue_abierto": sigue,
            "ficha_sin_cambios": despues == antes, "usuario": usuario,
            "mensajes": _mensajes_error(resp, [l for l in re.split(r"\s{2,}", _plano(resp)) if l])[:3],
            "contrasena": "la temporal guardada en stod/clave_inicial (no se muestra)",
            "nota": "El usuario debe cambiarla en su primer ingreso. Verifique con él que puede entrar."}


if __name__ == "__main__":
    mcp.run()
