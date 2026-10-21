#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lucy.py — arranque, verificacion y append de la bitacora de continuidad.

Subcomandos:
  boot     verifica la cadena e imprime el bloque consolidado de arranque
  verify   verifica la cadena de hashes (exit 0 = OK, exit 1 = rota)
  add      agrega una entrada al final de BITACORA.md calculando su hash
  estado   imprime el estado volatil como clave=valor
  sello    imprime el sello canonico solo si la cadena verifica
  montaje  audita las precondiciones del montaje (sin abrir sesion)
  archivar mueve entradas antiguas a ARCHIVO-1.md y declara el ancla

El hash de cada entrada es (formula v1):
  sha256( prev_hash | id | ts | tipo | titulo | cuerpo )[0:12]
con prev_hash = GENESIS_LUCY_V3.1 para la primera entrada, y titulo/cuerpo
normalizados (NFC, espacios colapsados, sin extremos). La marca de tiempo entra
en el hash: alterar una fecha tambien rompe la cadena.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BITACORA = HERE / "BITACORA.md"
ESTADO = HERE / "ESTADO.md"
CANARIO = HERE / "_canario.json"
ARCHIVO1 = HERE / "ARCHIVO-1.md"

GENESIS = "GENESIS_LUCY_V3.1"
SELLO = "SELLO_ADN_LUCY_OK_V3.1"
TIPOS = ("HITO", "DECISION", "CORRECCION", "NOTA")

ENTRY_RE = re.compile(r"^##\s+(?P<id>L-\d{3,})\s+·\s+(?P<ts>.+?)\s+·\s+(?P<tipo>[A-Z]+)\s*$")
BLOQUE_RE = re.compile(r"^##\s+(?P<id>L-\d{3,})\s+·", re.M)
FIELD_RE = re.compile(r"^(?P<key>Titulo|Cuerpo|Tags|Hash):\s*(?P<val>.*)$")
SCOPE_RE = re.compile(r"<!--\s*estado:inicio\s*-->(.*?)<!--\s*estado:fin\s*-->", re.S)
CADENA_RE = re.compile(r"<!--\s*cadena:inicio\s*-->(.*?)<!--\s*cadena:fin\s*-->", re.S)

CABECERA_ARCHIVO = (
    "# ARCHIVO-1 — Lucy v3.1\n\n"
    "Entradas retiradas de BITACORA.md por `lucy.py archivar`. Append-only: lo archivado no se\n"
    "edita ni se purga. La cadena completa se verifica desde GENESIS_LUCY_V3.1; el ultimo hash\n"
    "de este archivo es el ancla que declare BITACORA.md.\n"
)


class ErrorBitacora(Exception):
    pass


def normalizar(texto: str) -> str:
    """NFC + espacios colapsados + sin extremos. Base del hash y de la comparacion."""
    texto = unicodedata.normalize("NFC", texto or "")
    return re.sub(r"\s+", " ", texto).strip()


def calcular_hash(prev: str, ident: str, ts: str, tipo: str, titulo: str, cuerpo: str) -> str:
    crudo = "|".join((prev, ident, normalizar(ts), tipo, normalizar(titulo), normalizar(cuerpo)))
    return hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:12]


def leer_texto(path: Path) -> str:
    if not path.exists():
        raise ErrorBitacora(f"no existe {path}")
    return path.read_text(encoding="utf-8-sig")


def escribir_atomico(path: Path, texto: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(texto)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def parsear_entradas(texto: str) -> list[dict]:
    entradas: list[dict] = []
    actual: dict | None = None
    ultimo_campo: str | None = None

    for crudo in texto.splitlines():
        linea = crudo.rstrip()
        m = ENTRY_RE.match(linea.strip())
        if m:
            if actual:
                entradas.append(actual)
            actual = {
                "id": m.group("id"),
                "ts": m.group("ts").strip(),
                "tipo": m.group("tipo"),
                "titulo": "",
                "cuerpo": "",
                "tags": "",
                "hash": "",
                "linea": 0,
            }
            ultimo_campo = None
            continue
        if actual is None:
            continue
        if not linea.strip():
            ultimo_campo = None
            continue
        fm = FIELD_RE.match(linea.strip())
        if fm:
            clave = fm.group("key").lower()
            actual[clave] = fm.group("val").strip()
            ultimo_campo = clave
        elif linea.startswith(" ") and ultimo_campo in ("titulo", "cuerpo"):
            # continuacion de un campo multilinea
            actual[ultimo_campo] = (actual[ultimo_campo] + " " + linea.strip()).strip()

    if actual:
        entradas.append(actual)
    return entradas


def verificar(entradas: list[dict], ancla: str = GENESIS) -> list[str]:
    """Devuelve lista de errores. Vacia = cadena integra desde `ancla`."""
    errores: list[str] = []
    prev = ancla
    anterior_num = 0

    for i, e in enumerate(entradas, start=1):
        ident = e["id"]
        if e["tipo"] not in TIPOS:
            errores.append(f"{ident}: tipo desconocido '{e['tipo']}' (validos: {', '.join(TIPOS)})")
        if not e["hash"]:
            errores.append(f"{ident}: falta la linea Hash:")
            prev = "<sin-hash>"
            continue
        esperado = calcular_hash(prev, ident, e["ts"], e["tipo"], e["titulo"], e["cuerpo"])
        if esperado != e["hash"]:
            errores.append(
                f"{ident}: hash no coincide (archivo={e['hash']} calculado={esperado}) "
                f"— contenido editado o entrada fuera de orden"
            )
        try:
            num = int(ident.split("-")[1])
            if num <= anterior_num:
                errores.append(f"{ident}: id no es creciente respecto de L-{anterior_num:03d}")
            anterior_num = max(anterior_num, num)
        except (IndexError, ValueError):
            errores.append(f"{ident}: id con formato invalido")
        prev = e["hash"]

    return errores


def leer_estado() -> dict[str, str]:
    if not ESTADO.exists():
        return {}
    texto = leer_texto(ESTADO)
    m = SCOPE_RE.search(texto)
    bloque = m.group(1) if m else texto
    datos: dict[str, str] = {}
    for linea in bloque.splitlines():
        if ":" not in linea:
            continue
        clave, _, valor = linea.partition(":")
        clave = clave.strip().lower()
        if clave and not clave.startswith("#") and not clave.startswith("-"):
            datos[clave] = valor.strip()
    return datos


# --------------------------------------------------------------------------- #
# Archivo y ancla de la cadena
# --------------------------------------------------------------------------- #

def _meta_de_texto(texto: str) -> dict:
    m = CADENA_RE.search(texto or "")
    if not m:
        return {}
    datos: dict = {}
    for linea in m.group(1).splitlines():
        if ":" not in linea:
            continue
        clave, _, valor = linea.partition(":")
        clave = clave.strip().lower()
        if clave and not clave.startswith("#"):
            datos[clave] = valor.strip()
    return datos


def leer_cadena_meta() -> dict:
    """Ancla declarada en BITACORA.md; {} si la bitacora no se ha cortado nunca."""
    if not BITACORA.exists():
        return {}
    return _meta_de_texto(leer_texto(BITACORA))


def bloques_entrada(texto: str) -> list[dict]:
    """Segmenta el texto por cabeceras '## L-xxx' conservando el texto crudo."""
    marcas = list(BLOQUE_RE.finditer(texto))
    bloques = []
    for i, marca in enumerate(marcas):
        bloques.append({
            "id": marca.group("id"),
            "inicio": marca.start(),
            "fin": marcas[i + 1].start() if i + 1 < len(marcas) else len(texto),
        })
    return bloques


def con_bloque_cadena(cabecera: str, ancla: str, archivo: str, archivadas: int) -> str:
    """Reemplaza o inserta el bloque de ancla en la cabecera de BITACORA.md."""
    base = CADENA_RE.sub("", cabecera or "").rstrip()
    bloque = (
        "<!-- cadena:inicio -->\n"
        f"ancla: {ancla}\n"
        f"archivo: {archivo}\n"
        f"archivadas: {archivadas}\n"
        "<!-- cadena:fin -->"
    )
    return base + "\n\n" + bloque + "\n\n"


def verificar_todo() -> tuple[list[str], dict]:
    """Verifica el tramo activo y, si hay ancla, tambien ARCHIVO-1.md.

    El corte mueve entradas, no las borra: la cadena completa sigue siendo
    GENESIS..ultimo hash del archivo + ancla..ultimo hash activo. Sin esto,
    `verify` sobre el archivo activo empezaria en GENESIS y fallaria en el
    primer elemento tras el corte.
    """
    meta = leer_cadena_meta()
    activas = parsear_entradas(leer_texto(BITACORA))
    ancla = meta.get("ancla")
    archivo = meta.get("archivo") or ARCHIVO1.name
    errores: list[str] = []
    archivadas: list[dict] = []

    if ancla:
        ruta = HERE / archivo
        if not ruta.exists():
            errores.append(f"el ancla declara {archivo}, que no existe")
        else:
            archivadas = parsear_entradas(leer_texto(ruta))
            if not archivadas:
                errores.append(f"{archivo} no tiene entradas")
            else:
                errores.extend(verificar(archivadas, GENESIS))
                if archivadas[-1]["hash"] != ancla:
                    errores.append(
                        f"ancla declarada {ancla} != ultimo hash de {archivo} ({archivadas[-1]['hash']})"
                    )
        errores.extend(verificar(activas, ancla))
    else:
        errores.extend(verificar(activas, GENESIS))

    return errores, {
        "activas": len(activas),
        "archivadas": len(archivadas),
        "total": len(activas) + len(archivadas),
        "ancla": ancla,
        "archivo": archivo if ancla else None,
    }


# --------------------------------------------------------------------------- #
# Canario del gancho de arranque
# --------------------------------------------------------------------------- #

def leer_canario() -> dict | None:
    """Ultima huella dejada por lucy/hooks/session_start.py, o None si no hay."""
    if not CANARIO.exists():
        return None
    try:
        datos = json.loads(leer_texto(CANARIO))
        return datos if isinstance(datos, dict) else None
    except Exception:
        return None


def edad_horas(canario: dict) -> float | None:
    """Horas transcurridas desde el ultimo arranque registrado."""
    epoch = canario.get("epoch")
    if isinstance(epoch, (int, float)):
        return (datetime.now(timezone.utc).timestamp() - float(epoch)) / 3600.0
    ts = canario.get("ts")
    if isinstance(ts, str):
        try:
            marca = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - marca).total_seconds() / 3600.0
        except ValueError:
            return None
    return None


def diagnosticar_canario(max_age_h: float) -> dict:
    """Estado del canario en tres valores sin ambiguedad.

    - sin_registro: el gancho no ha corrido nunca en este workspace (desmontado,
      ruta movida, config mal apuntada).
    - rancio: corrio, pero no en la ventana esperada.
    - fresco: corrio dentro de la ventana.
    """
    canario = leer_canario()
    if canario is None:
        return {
            "estado": "sin_registro",
            "max_age_h": max_age_h,
            "motivo": "no existe lucy/_canario.json",
        }
    edad = edad_horas(canario)
    if edad is None:
        return {
            "estado": "rancio",
            "max_age_h": max_age_h,
            "motivo": "huella ilegible (sin epoch ni ts parseable)",
            "canario": canario,
        }
    estado = "fresco" if -0.05 <= edad <= max_age_h else "rancio"
    return {
        "estado": estado,
        "max_age_h": max_age_h,
        "edad_h": round(edad, 2),
        "restante_h": round(max_age_h - edad, 2),
        "canario": canario,
    }


# --------------------------------------------------------------------------- #
# Auditoria del montaje (precondiciones, sin modelo)
# --------------------------------------------------------------------------- #

ARCHIVOS_MINIMOS = (
    "AGENTS.md",
    "lucy/BITACORA.md",
    "lucy/ESTADO.md",
    "lucy/lucy.py",
    "lucy/hooks/hooks.json",
    "lucy/hooks/session_start.py",
    "lucy/hooks/session-start.ps1",
)


def normalizar_ruta(texto: str) -> str:
    """Normaliza rutas para comparar sin atarse a mayusculas ni al separador."""
    valor = (texto or "").strip().strip("'\"").replace("/", "\\").rstrip("\\")
    return os.path.normcase(valor)


def lineas_sin_comentario(texto: str) -> list[str]:
    """Quita lineas de comentario YAML para que un bloque comentado no cuente."""
    return [linea.rstrip() for linea in texto.splitlines() if not linea.lstrip().startswith("#")]


def bloque_por_id(lineas: list[str], ident: str) -> list[str] | None:
    """Devuelve el cuerpo de `- id: <ident>` en un patch YAML.

    No es un parser YAML: corta en la siguiente linea con indentacion menor o
    igual a la del id. Suficiente para auditar cordis.patch.yml sin dependencias.
    """
    patron = re.compile(rf"^(\s*)- id:\s*{re.escape(ident)}\s*$")
    for i, linea in enumerate(lineas):
        m = patron.match(linea)
        if not m:
            continue
        base = len(m.group(1))
        cuerpo: list[str] = []
        for siguiente in lineas[i + 1:]:
            if not siguiente.strip():
                continue
            sangria = len(siguiente) - len(siguiente.lstrip())
            if sangria <= base:
                break
            cuerpo.append(siguiente)
        return cuerpo
    return None


def buscar_clave(cuerpo: list[str] | None, clave: str) -> str | None:
    if not cuerpo:
        return None
    patron = re.compile(rf"^\s*{re.escape(clave)}:\s*(.+?)\s*$")
    for linea in cuerpo:
        m = patron.match(linea)
        if m:
            return m.group(1)
    return None


def localizar_perfil(ruta: str | None) -> Path | None:
    """Encuentra el patch del perfil: argumento, DSH_PROFILE_DIR o ~/.dsh/profiles."""
    if ruta:
        candidato = Path(ruta)
        return candidato if candidato.exists() else None
    entorno = os.environ.get("DSH_PROFILE_DIR")
    if entorno:
        candidato = Path(entorno) / "cordis.patch.yml"
        if candidato.exists():
            return candidato
    raiz = Path.home() / ".dsh" / "profiles"
    if not raiz.is_dir():
        return None
    candidatos = sorted(raiz.glob("*/cordis.patch.yml"))
    for candidato in candidatos:
        if candidato.parent.name == "desktop":
            return candidato
    return candidatos[0] if candidatos else None


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #

def cmd_verify(args) -> int:
    errores, meta = verificar_todo()
    if args.json:
        print(json.dumps(
            {
                "ok": not errores,
                "entradas": meta["total"],
                "archivadas": meta["archivadas"],
                "errores": errores,
            },
            ensure_ascii=False, indent=2,
        ))
    else:
        if errores:
            print(f"CADENA ROTA — {len(errores)} problema(s) en {meta['total']} entrada(s):")
            for err in errores:
                print(f"  ! {err}")
        else:
            linea = f"CADENA OK — {meta['total']} entrada(s) verificada(s) desde {GENESIS}"
            if meta["archivadas"]:
                linea += f" · {meta['archivadas']} archivada(s) en {meta['archivo']}"
            print(linea)
    return 0 if not errores else 1


def cmd_boot(args) -> int:
    errores, meta = verificar_todo()
    estado = leer_estado()
    activas = parsear_entradas(leer_texto(BITACORA))

    print("=== ARRANQUE LUCY v3.1 ===")
    sufijo = f" · {meta['archivadas']} archivadas en {meta['archivo']}" if meta["archivadas"] else ""
    print(f"integridad : {'OK' if not errores else 'ROTA'} — {meta['total']} entrada(s){sufijo}")
    if errores:
        for err in errores:
            print(f"  ! {err}")
    claves = ("version", "piloto", "actualizado", "objetivo_activo", "frente", "proximo_paso")
    ancho = max((len(c) for c in claves if c in estado), default=0)
    for clave in claves:
        if clave in estado:
            print(f"{clave:<{ancho}} : {estado[clave]}")

    if activas:
        print("\nultimas entradas:")
        for e in activas[-args.ultimas:]:
            print(f"  {e['id']} [{e['tipo']}] {e['ts']} — {e['titulo']}")

    diag = diagnosticar_canario(24.0)
    if diag["estado"] == "sin_registro":
        print("\ncanario: SIN REGISTRO — el gancho de arranque no corre en este workspace")
    else:
        edad = diag.get("edad_h")
        cuando = "?" if edad is None else f"hace {edad:.2f} h"
        etiqueta = "fresco" if diag["estado"] == "fresco" else "RANCIO"
        print(f"\ncanario: {etiqueta}, {cuando} ({diag.get('canario', {}).get('ts', '?')})")

    if not errores:
        print(f"sello: {SELLO}")
    return 0 if not errores else 1


def cmd_doctor(args) -> int:
    """Diagnostica si el gancho de arranque sigue vivo.

    Codigos: 0 = canario fresco y cadena OK · 1 = canario rancio ·
             2 = sin registro (el gancho nunca corrio) · 3 = cadena rota.
    """
    errores, meta = verificar_todo()
    diag = diagnosticar_canario(args.max_age_h)
    estado = diag["estado"]

    if args.json:
        print(json.dumps(
            {
                "canario": diag,
                "cadena_ok": not errores,
                "errores": errores,
                "entradas": meta["total"],
                "archivadas": meta["archivadas"],
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print("=== CANARIO DE ARRANQUE LUCY ===")
        if estado == "sin_registro":
            print("estado    : SIN REGISTRO — el gancho de arranque no ha corrido nunca aquí")
            print("            lucy/_canario.json no existe o es ilegible.")
        else:
            c = diag.get("canario", {})
            edad = diag.get("edad_h")
            restante = diag.get("restante_h")
            etiqueta = "FRESCO" if estado == "fresco" else "RANCIO"
            detalle = ""
            if edad is not None:
                detalle = f" (hace {edad:.2f} h"
                if restante is not None:
                    detalle += f" · vence en {max(restante, 0.0):.2f} h"
                detalle += ")"
            print(f"estado    : {etiqueta}{detalle}")
            print(f"ultimo_ts : {c.get('ts', '?')}")
            origen = c.get("source") or "?"
            sesion = c.get("session_id") or "?"
            print(f"sesion    : {origen} · {sesion}")
            if c.get("cwd"):
                print(f"cwd       : {c.get('cwd')}")
            print(f"en arranque: {'OK' if c.get('cadena_ok') else 'ROTA'} · {c.get('entradas', '?')} entrada(s)")
            if c.get("python"):
                print(f"python    : {c.get('python')}")
            if not (c.get("session_id") or "").strip() or not (c.get("cwd") or "").strip():
                print("aviso     : huella sin sesion o sin cwd — parece una ejecucion manual del")
                print("            gancho, no un arranque real. No la tomes como prueba de montaje.")
        print(f"cadena ahora: {'OK' if not errores else 'ROTA'} · {meta['total']} entrada(s)")
        if errores:
            for err in errores:
                print(f"  ! {err}")
        if estado != "fresco" or errores:
            print()
            print("Qué significa: si acabas de abrir una sesión y NO viste el bloque <lucy-boot>,")
            print("el montaje del gancho está roto. Revisa ~/.dsh/profiles/desktop/cordis.patch.yml")
            print("y la guarda de lucy/hooks/session-start.ps1. El arranque manual (AGENTS.md §1.b)")
            print("sigue siendo válido mientras tanto.")
            print("Si el gancho corre y aun así no hay huella, revisa sandbox-policy.workspaceRoot")
            print("en el perfil: un agentless call no hereda el workspace de la sesión (L-009).")

    if errores:
        return 3
    if estado == "sin_registro":
        return 2
    if estado == "rancio":
        return 1
    return 0


def cmd_montaje(args) -> int:
    """Audita las precondiciones del montaje sin abrir sesion ni depender del modelo.

    No juzga la edad del canario: dias sin sesion son normales y un umbral fijo
    daria RANCIO falso en el unico caso en que la vigilancia deberia callarse.
    Codigos: 0 = montaje OK · 1 = problemas · 2 = perfil no localizado (incompleta).
    """
    workspace = HERE.parent
    checks: list[dict] = []
    problemas: list[str] = []
    avisos: list[str] = []

    def registrar(ident: str, ok: bool, detalle: str) -> None:
        checks.append({"id": ident, "ok": bool(ok), "detalle": detalle})
        if not ok:
            problemas.append(f"{ident}: {detalle}")

    faltantes = [rel for rel in ARCHIVOS_MINIMOS if not (workspace / rel).exists()]
    registrar(
        "archivos",
        not faltantes,
        "todos presentes" if not faltantes else "faltan: " + ", ".join(faltantes),
    )

    try:
        errores, meta = verificar_todo()
    except ErrorBitacora as exc:
        errores, meta = [str(exc)], {"activas": 0, "archivadas": 0, "total": 0, "ancla": None, "archivo": None}
    sufijo = f" · {meta['archivadas']} archivadas" if meta["archivadas"] else ""
    registrar(
        "cadena",
        not errores,
        f"{meta['total']} entrada(s) OK{sufijo}" if not errores else "; ".join(errores),
    )

    estado = leer_estado()
    faltan_estado = [k for k in ("objetivo_activo", "frente", "proximo_paso") if not estado.get(k)]
    if faltan_estado:
        checks.append({"id": "estado", "ok": False, "detalle": "faltan claves: " + ", ".join(faltan_estado)})
        avisos.append("estado: faltan " + ", ".join(faltan_estado) + " (telemetria degradada, no montaje roto)")
    else:
        checks.append({"id": "estado", "ok": True, "detalle": "objetivo, frente y proximo paso presentes"})

    canario = leer_canario()
    if canario is None:
        registrar("canario", False, "no existe o es ilegible lucy/_canario.json")
    else:
        edad = edad_horas(canario)
        detalle = str(canario.get("ts", "?"))
        if edad is not None:
            detalle += f" · hace {edad:.2f} h (edad es dato, no veredicto)"
        registrar("canario", True, detalle)
        if not (canario.get("session_id") or "").strip() or not (canario.get("cwd") or "").strip():
            avisos.append("canario: huella sin sesion o sin cwd — parece ejecucion manual del gancho")

    perfil = localizar_perfil(args.perfil)
    incompleta = perfil is None
    if perfil is None:
        checks.append({"id": "perfil", "ok": False, "nd": True, "detalle": "no se localizo cordis.patch.yml"})
        avisos.append("perfil: no localizado; el montaje no se pudo auditar (pasa --perfil <ruta>)")
    else:
        lineas = lineas_sin_comentario(leer_texto(perfil))
        esperado = normalizar_ruta(str(workspace / "lucy" / "hooks" / "hooks.json"))
        cuerpo = bloque_por_id(lineas, "hooks-claude-code")
        if cuerpo is None:
            registrar("hooks-claude-code", False, "no hay entrada 'hooks-claude-code' en el perfil")
        else:
            config_path = buscar_clave(cuerpo, "configPath")
            if config_path is None:
                registrar("hooks-claude-code", False, "la entrada no declara configPath")
            elif normalizar_ruta(config_path) != esperado:
                registrar("hooks-claude-code", False, f"configPath={config_path}; esperado {esperado}")
            elif not Path(config_path.strip().strip("'\"")).exists():
                registrar("hooks-claude-code", False, "configPath no resuelve a un archivo existente")
            else:
                registrar("hooks-claude-code", True, "configPath resuelve")

        cuerpo = bloque_por_id(lineas, "sandbox-policy")
        if cuerpo is None:
            registrar(
                "sandbox-policy",
                False,
                "no hay entrada 'sandbox-policy' (L-009: el canario no se escribiria)",
            )
        else:
            root = buscar_clave(cuerpo, "workspaceRoot")
            modo = (buscar_clave(cuerpo, "mode") or "").strip().strip("'\"")
            esperado = normalizar_ruta(str(workspace))
            if root is None:
                registrar("sandbox-policy", False, "no declara workspaceRoot")
            elif normalizar_ruta(root) != esperado:
                registrar(
                    "sandbox-policy",
                    False,
                    f"workspaceRoot={root}; esperado {esperado} (el canario no se escribiria)",
                )
            elif modo != "workspace-write":
                registrar("sandbox-policy", False, f"mode={modo or '?'}; se espera workspace-write")
            else:
                registrar("sandbox-policy", True, "root y modo correctos")

    ok = not problemas
    if args.json:
        print(json.dumps(
            {
                "ok": ok,
                "incompleta": incompleta,
                "workspace": str(workspace),
                "perfil": str(perfil) if perfil else None,
                "checks": checks,
                "problemas": problemas,
                "avisos": avisos,
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print("=== MONTAJE LUCY ===")
        print(f"workspace : {workspace}")
        for c in checks:
            marca = "N/D  " if c.get("nd") else ("OK   " if c["ok"] else "FALLO")
            print(f"{marca} {c['id']:<18} {c['detalle']}")
        if perfil is not None:
            print(f"perfil    : {perfil}")
        for aviso in avisos:
            print(f"aviso     : {aviso}")
        if problemas:
            print(f"veredicto : MONTAJE ROTO — {len(problemas)} problema(s)")
        elif incompleta:
            print("veredicto : MONTAJE INCOMPLETO — sin perfil no hay nada que auditar del montaje")
        else:
            print("veredicto : MONTAJE OK")
        if incompleta:
            print("nota      : pasa --perfil <ruta a cordis.patch.yml> para completar la auditoria")

    if problemas:
        return 1
    if perfil is None:
        return 2
    return 0


def cmd_archivar(args) -> int:
    """Mueve entradas antiguas a ARCHIVO-1.md y declara el ancla en BITACORA.md.

    El corte no borra: reescribe el tramo activo y declara `ancla` = hash de la
    ultima entrada movida, de modo que `verify` encadena GENESIS..archivo..activo.
    Se niega a cortar sobre una cadena rota o si dejaria la bitacora activa vacia.
    """
    texto = leer_texto(BITACORA)
    meta = leer_cadena_meta()
    errores, _ = verificar_todo()
    if errores:
        raise ErrorBitacora("se niega a archivar sobre una cadena rota:\n  " + "\n  ".join(errores))

    if ARCHIVO1.exists() and not meta.get("ancla"):
        raise ErrorBitacora(
            f"{ARCHIVO1.name} existe pero BITACORA.md no declara ancla: resolucion manual "
            "(no se sobrescribe un archivo con entradas historicas)"
        )

    bloques = bloques_entrada(texto)
    ids = [b["id"] for b in bloques]
    if not ids:
        raise ErrorBitacora("BITACORA.md no tiene entradas")
    if args.hasta not in ids:
        raise ErrorBitacora(f"{args.hasta} no esta en BITACORA.md (ids: {ids[0]}..{ids[-1]})")
    corte = ids.index(args.hasta) + 1
    if corte >= len(ids):
        raise ErrorBitacora("el corte dejaria la bitacora activa vacia; usa un id anterior")

    movidos = bloques[:corte]
    quedan = bloques[corte:]
    archivadas_previas = len(parsear_entradas(leer_texto(ARCHIVO1))) if ARCHIVO1.exists() else 0
    ancla = parsear_entradas(texto[movidos[-1]["inicio"]:movidos[-1]["fin"]])[-1]["hash"]

    cabecera = texto[:bloques[0]["inicio"]]
    nuevo_activo = con_bloque_cadena(cabecera, ancla, ARCHIVO1.name, archivadas_previas + len(movidos))
    nuevo_activo += texto[quedan[0]["inicio"]:]

    segmentos = [texto[b["inicio"]:b["fin"]].rstrip() for b in movidos]
    prefijo = (leer_texto(ARCHIVO1).rstrip() + "\n\n") if ARCHIVO1.exists() else (CABECERA_ARCHIVO + "\n")
    nuevo_archivo = prefijo + "\n\n".join(segmentos) + "\n"

    # Validacion en memoria antes de tocar disco: el corte es un movimiento, no un borrado.
    errores_arch = verificar(parsear_entradas(nuevo_archivo), GENESIS)
    if errores_arch:
        raise ErrorBitacora("el archivo resultante no verifica:\n  " + "\n  ".join(errores_arch))
    errores_act = verificar(parsear_entradas(nuevo_activo), ancla)
    if errores_act:
        raise ErrorBitacora("la bitacora resultante no verifica:\n  " + "\n  ".join(errores_act))

    if args.dry_run:
        print("=== ARCHIVAR LUCY (simulacion) ===")
    else:
        escribir_atomico(ARCHIVO1, nuevo_archivo)
        escribir_atomico(BITACORA, nuevo_activo)
        errores_final, _ = verificar_todo()
        if errores_final:
            raise ErrorBitacora(
                "se escribio pero la verificacion posterior fallo:\n  " + "\n  ".join(errores_final)
            )
        print("=== ARCHIVAR LUCY ===")

    print(f"corte     : {movidos[0]['id']}..{movidos[-1]['id']} ({len(movidos)} entradas) -> {ARCHIVO1.name}")
    print(f"activas   : {quedan[0]['id']}..{quedan[-1]['id']} ({len(quedan)} entradas)")
    print(f"ancla     : {ancla}")
    if args.dry_run:
        print("simulacion: no se escribio nada")
    return 0


def cmd_estado(args) -> int:
    datos = leer_estado()
    if args.json:
        print(json.dumps(datos, ensure_ascii=False, indent=2))
    else:
        for clave, valor in datos.items():
            print(f"{clave}={valor}")
    return 0


def cmd_sello(args) -> int:
    errores, _ = verificar_todo()
    if errores:
        print(f"sello DENEGADO — cadena rota ({len(errores)} problema(s))", file=sys.stderr)
        for err in errores:
            print(f"  ! {err}", file=sys.stderr)
        return 1
    print(SELLO)
    return 0


def cmd_add(args) -> int:
    texto = leer_texto(BITACORA)
    entradas = parsear_entradas(texto)
    errores, meta = verificar_todo()
    if errores:
        raise ErrorBitacora(
            "se niega a anexar sobre una cadena rota; corrige primero:\n  "
            + "\n  ".join(errores)
        )
    if not entradas and meta.get("ancla"):
        raise ErrorBitacora(
            "BITACORA.md no tiene entradas activas y declara un ancla: el corte dejo la bitacora "
            "vacia (archivar nunca debe hacerlo). Restaura desde el respaldo o corrige el ancla."
        )

    tipo = args.tipo.upper()
    if tipo not in TIPOS:
        raise ErrorBitacora(f"tipo invalido '{args.tipo}' (validos: {', '.join(TIPOS)})")

    cuerpo = args.cuerpo if args.cuerpo is not None else sys.stdin.read()
    titulo_n = normalizar(args.titulo)
    cuerpo_n = normalizar(cuerpo)
    if not titulo_n:
        raise ErrorBitacora("el titulo no puede estar vacio")

    prev = entradas[-1]["hash"] if entradas else GENESIS
    siguiente = (int(entradas[-1]["id"].split("-")[1]) + 1) if entradas else 1
    ident = f"L-{siguiente:03d}"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    h = calcular_hash(prev, ident, ts, tipo, titulo_n, cuerpo_n)

    lineas = ["", f"## {ident} · {ts} · {tipo}", f"Titulo: {titulo_n}", f"Cuerpo: {cuerpo_n}"]
    if args.tags:
        lineas.append("Tags: " + ", ".join(t.strip() for t in args.tags.split(",") if t.strip()))
    lineas.append(f"Hash: {h}")

    base = texto if texto.endswith("\n") else texto + "\n"
    escribir_atomico(BITACORA, base + "\n".join(lineas) + "\n")

    if args.json:
        print(json.dumps({"id": ident, "hash": h, "tipo": tipo, "ts": ts}, ensure_ascii=False))
    else:
        print(f"{ident} anexada · {h} · cadena {meta['total'] + 1} entrada(s)")
    return 0


# --------------------------------------------------------------------------- #

def procesar_synapse(path: Path) -> dict:
    """Valida sintaxis, claves obligatorias y firma SHA-256 de un paquete .synapse."""
    try:
        contenido = path.read_text(encoding="utf-8-sig")
    except Exception as exc:
        raise ErrorBitacora(f"No se pudo leer el archivo: {exc}")

    try:
        data = json.loads(contenido)
    except json.JSONDecodeError:
        raise ErrorBitacora(f"Sintaxis invalida: '{path.name}' no es un JSON valido")

    if data.get("protocolo") != "LUCY_SYNAPSE_V1":
        raise ErrorBitacora(f"Protocolo no compatible: '{data.get('protocolo')}'")

    campos_requeridos = ("emisor", "vector", "payload", "firma_sha256")
    for campo in campos_requeridos:
        if campo not in data:
            raise ErrorBitacora(f"Paquete incompleto: falta la clave obligatoria '{campo}'")

    # Calculo determinista de firma sobre el payload serializado
    payload_str = json.dumps(data["payload"], sort_keys=True, ensure_ascii=False)
    firma_calculada = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()[:12]

    if firma_calculada != data["firma_sha256"]:
        raise ErrorBitacora(
            f"Firma invalida (esperada={data['firma_sha256']} calculada={firma_calculada})"
        )

    return data


def cmd_importar(args) -> int:
    path_archivo = Path(args.archivo)
    if not path_archivo.is_file():
        raise ErrorBitacora(f"El archivo no existe en la ruta: {args.archivo}")

    paquete = procesar_synapse(path_archivo)

    emisor = paquete["emisor"].get("nodo_id", "desconocido")
    vector = paquete.get("vector", "DESCONOCIDO")
    tesis = paquete["payload"].get("tesis", "Sin tesis declarada")
    firma = paquete.get("firma_sha256")

    print(f"=== CUARENTENA SUPERADA ===")
    print(f"Emisor        : {emisor}")
    print(f"Vector        : {vector}")
    print(f"Firma SHA-256 : {firma} (VALIDA)")
    print(f"Tesis         : {tesis}")
    print(f"\nPara incorporar este hito al registro inmutable, ejecuta:")
    print(f'  python lucy/lucy.py add --tipo NOTA --titulo "Synapse de {emisor}" --cuerpo "{tesis}"')
    return 0
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lucy.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="verificar la cadena de hashes")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_verify)

    b = sub.add_parser("boot", help="bloque consolidado de arranque")
    b.add_argument("--ultimas", type=int, default=3)
    b.set_defaults(func=cmd_boot)

    e = sub.add_parser("estado", help="estado volatil como clave=valor")
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_estado)

    d = sub.add_parser("doctor", help="diagnosticar el canario del gancho de arranque")
    d.add_argument("--max-age-h", type=float, default=24.0, help="ventana de frescura en horas (default 24)")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_doctor)

    s = sub.add_parser("sello", help="sello canonico si la cadena verifica")
    s.set_defaults(func=cmd_sello)

    m = sub.add_parser("montaje", help="auditar las precondiciones del montaje")
    m.add_argument("--perfil", default=None, help="ruta a cordis.patch.yml (por defecto DSH_PROFILE_DIR o desktop)")
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_montaje)

    r = sub.add_parser("archivar", help="mover entradas antiguas a ARCHIVO-1.md y declarar el ancla")
    r.add_argument("--hasta", required=True, help="id de la ultima entrada a archivar (p. ej. L-006)")
    r.add_argument("--dry-run", action="store_true", help="mostrar el corte sin escribir")
    r.set_defaults(func=cmd_archivar)

    a = sub.add_parser("add", help="anexar una entrada")
    a.add_argument("--tipo", required=True, choices=[t.lower() for t in TIPOS] + list(TIPOS))
    a.add_argument("--titulo", required=True)
    a.add_argument("--cuerpo", default=None, help="si se omite, se lee de stdin")
    a.add_argument("--tags", default="")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_add)
    imp = sub.add_parser("importar", help="audita y valida un archivo .synapse en cuarentena")
    imp.add_argument("archivo", help="ruta al archivo .synapse")
    imp.set_defaults(func=cmd_importar)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except ErrorBitacora as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
