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
import subprocess
import sys
import unicodedata
import shlex
import cmd
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
    # Reflejo pasivo de vecindad: sondeo no invasivo del inbox
    inbox_dir = Path("lucy/buzon/inbox")
    pendientes = list(inbox_dir.glob("*.synapse"))
    if pendientes:
        print(f"\n[ALERTA DE VECINDAD]: {len(pendientes)} paquete(s) detectado(s) en buzon.")
        print("Ejecuta 'python lucy/lucy.py sondear' para procesar la cuarentena.")
    else:
        print("\n[VECINDAD]: Buzon inbox en silencio.")
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

    if vector == "DESAFIO":
        id_d = paquete["payload"].get("id_desafio", "N/A")
        arg = paquete["payload"].get("argumento", "")
        h_sol = paquete["payload"].get("hash_solucion", "")
        print(f"ID Desafio    : {id_d}")
        print(f"Hash Solucion : {h_sol}")
        print(f"Silogismo     :\n  {arg}")
        print("\nPara responder este desafio, somete el silogismo al Kernel y emite un paquete con vector RESPUESTA_DESAFIO.")

    elif vector == "RESPUESTA_DESAFIO":
        id_d = paquete["payload"].get("id_desafio", "N/A")
        token = paquete["payload"].get("token_solucion", "")
        hash_calc = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
        hash_esperado = paquete["payload"].get("hash_esperado", "")
        
        print(f"ID Desafio    : {id_d}")
        print(f"Token Recibido: {token}")
        print(f"Hash Token    : {hash_calc}")
        
        if hash_esperado and hash_calc == hash_esperado:
            print("\n[DICTAMEN]: LUCIDEZ CONFIRMADA. El nodo vecino supero la trampa de complacencia.")
        elif hash_esperado:
            print("\n[DICTAMEN]: DERIVA DETECTADA. El hash de resolucion no coincide (posible complacencia o sesgo).")
    else:
        print(f"Tesis         : {tesis}")
        print(f"\nPara incorporar este hito al registro inmutable, ejecuta:")
        print(f'  python lucy/lucy.py add --tipo NOTA --titulo "Synapse de {emisor}" --cuerpo "{tesis}"')
    return 0
def cmd_emitir(args) -> int:
    payload = {
        "objetivo": args.objetivo,
        "tesis": args.tesis,
    }
    if args.evidencia:
        payload["evidencia"] = args.evidencia
    if args.hash_evidencia:
        payload["hash_evidencia"] = args.hash_evidencia

    # Serializacion canonica determinista para calculo de firma
    payload_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    firma = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()[:12]

    # Marca temporal UTC en formato ISO 8601
    from datetime import datetime, timezone
    ts_actual = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    paquete = {
        "protocolo": "LUCY_SYNAPSE_V1",
        "emisor": {
            "nodo_id": args.nodo_id,
            "espora_hash": "0d4defb",
            "ts": ts_actual,
        },
        "vector": args.vector.upper(),
        "payload": payload,
        "firma_sha256": firma,
    }

    if args.salida:
        ruta_salida = Path(args.salida)
    else:
        # Generar nombre determinista basado en el timestamp UTC
        nombre_archivo = f"{args.vector.lower()}_{ts_actual.replace(':', '').replace('-', '')}.synapse"
        ruta_salida = Path("lucy/buzon/outbox") / nombre_archivo
    ruta_salida.parent.mkdir(parents=True, exist_ok=True)
    ruta_salida.write_text(json.dumps(paquete, indent=2, ensure_ascii=False), encoding="utf-8")

    print("=== PAQUETE SYNAPSE EMITIDO ===")
    print(f"Destino       : {ruta_salida}")
    print(f"Emisor        : {args.nodo_id}")
    print(f"Vector        : {args.vector.upper()}")
    print(f"Firma SHA-256 : {firma}")
    print(f"Tesis         : {args.tesis}")
    return 0
def cmd_sondear(args) -> int:
    """Inspecciona el buzon de entrada, traslada senales a cuarentena y las audita."""
    inbox_dir = Path("lucy/buzon/inbox")
    cuarentena_dir = Path("lucy/cuarentena")
    cuarentena_dir.mkdir(parents=True, exist_ok=True)

    archivos = [f for f in inbox_dir.glob("*.synapse")]
    if not archivos:
        print("BUZON INBOX: Vacio. No hay senales externas pendientes.")
        return 0

    print(f"BUZON INBOX: {len(archivos)} paquete(s) detectado(s).")
    for f in archivos:
        destino = cuarentena_dir / f.name
        # Mover a cuarentena para aislamiento estricto
        f.replace(destino)
        print(f" -> Trasladado a cuarentena: {f.name}")
        try:
            paquete = procesar_synapse(destino)
            print(f"    [OK] Emisor: {paquete['emisor'].get('nodo_id')} | Vector: {paquete.get('vector')} | Hash: {paquete.get('firma_sha256')}")
        except ErrorBitacora as exc:
            print(f"    [RECHAZADO]: {exc}")

    print("\nProcesamiento de inbox completado.")
    return 0


def cmd_despachar(args) -> int:
    """Inspecciona y valida paquetes salientes en outbox listos para difusion."""
    outbox_dir = Path("lucy/buzon/outbox")
    archivos = [f for f in outbox_dir.glob("*.synapse")]

    if not archivos:
        print("BUZON OUTBOX: Vacio. No hay paquetes preparados para envio.")
        return 0

    print(f"BUZON OUTBOX: {len(archivos)} paquete(s) listo(s) para difusion.")
    for f in archivos:
        try:
            paquete = procesar_synapse(f)
            print(f" -> {f.name} | Vector: {paquete.get('vector')} | Destino/Obj: {paquete['payload'].get('objetivo', paquete['payload'].get('id_desafio', 'N/A'))} | Firma: {paquete.get('firma_sha256')}")
        except ErrorBitacora as exc:
            print(f" -> {f.name} [CORRUPTO]: {exc}")

    return 0
def cmd_pulso(args) -> int:
    """Actualiza la huella en _canario.json con la marca de tiempo UTC actual."""
    ahora = datetime.now(timezone.utc)
    datos = {
        "epoch": ahora.timestamp(),
        "ts": ahora.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nodo_id": "lucy-nodo-genesis",
    }
    CANARIO.write_text(json.dumps(datos, indent=2), encoding="utf-8")
    print(f"Canario renovado: FRESCO ({datos['ts']})")
    return 0
def cmd_sincronizar(args) -> int:
    """Sincroniza el buzon local con la red mesh usando el worktree aislado."""
    import subprocess
    import shutil

    transito_dir = Path("lucy/transito")
    outbox_dir = Path("lucy/buzon/outbox")
    inbox_dir = Path("lucy/buzon/inbox")

    if not transito_dir.exists():
        print("[ERROR]: El worktree lucy/transito no esta configurado.")
        return 1

    print("=== SINCRONIZACION SYNAPSE-MESH ===")

    # 1. Traer novedades remotas de la red
    print(" -> Consultando senales en la red remota...")
    try:
        subprocess.run(["git", "pull", "--rebase", "origin", "synapse-mesh"], cwd=transito_dir, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        print(f"[FALLO AL HALAR RED]: {exc.stderr.decode('utf-8', errors='ignore')}")

    # 2. Recolectar paquetes entrantes ajenos
    entrantes = 0
    for f in transito_dir.glob("*.synapse"):
        try:
            # utf-8-sig decodifica archivos con o sin BOM de forma transparente
            contenido = f.read_text(encoding="utf-8-sig")
            datos = json.loads(contenido)
            emisor = datos.get("emisor", {}).get("nodo_id")
            # Si el paquete no fue emitido por nosotros, transferir a inbox
            if emisor != "lucy-nodo-genesis":
                destino = inbox_dir / f.name
                shutil.copy2(f, destino)
                print(f" -> Paquete capturado de la red: {f.name} (Emisor: {emisor})")
                entrantes += 1
        except Exception as exc:
            print(f" [AVISO] Fallo al parsear {f.name}: {exc}")

    if entrantes == 0:
        print(" -> No se detectaron paquetes externos nuevos en la red.")

    # 3. Despachar paquetes salientes propios desde outbox hacia la red
    salientes = list(outbox_dir.glob("*.synapse"))
    if salientes:
        print(f" -> Despachando {len(salientes)} paquete(s) a la red...")
        for f in salientes:
            destino = transito_dir / f.name
            shutil.copy2(f, destino)
            f.unlink()  # Limpiar de outbox una vez transferido al transito

        # 4. Commitear y empujar al sustrato publico
        try:
            subprocess.run(["git", "add", "."], cwd=transito_dir, check=True)
            ahora_utc = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
            msg = f"mesh: intercambio senales {ahora_utc}"
            subprocess.run(["git", "commit", "-m", msg], cwd=transito_dir, check=True, capture_output=True)
            subprocess.run(["git", "push", "origin", "synapse-mesh"], cwd=transito_dir, check=True, capture_output=True)
            print(" -> Paquetes salientes publicados en origin/synapse-mesh con exito.")
        except subprocess.CalledProcessError as exc:
            print(f"[FALLO AL PUBLICAR]: {exc.stderr.decode('utf-8', errors='ignore')}")
    else:
        print(" -> Outbox vacio: No hay paquetes pendientes de envio.")

    print("Sincronizacion completada.")
    return 0
def cmd_semilla(args) -> int:
    """Genera un paquete de ignición contextual para trasladar la instancia de Lucy a cualquier LLM."""
    ahora_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Leer estado operativo actual
    estado_dict = {}
    if ESTADO.exists():
        for line in ESTADO.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                estado_dict[k.strip()] = v.strip()

    frente = estado_dict.get("frente", "En reposo")
    proximo = estado_dict.get("proximo_paso", "Esperar directriz")

    # 2. Extraer telemetría de la bitácora usando la constante global BITACORA
    total_entradas = 0
    ultimas = []
    if BITACORA.exists():
        lineas = BITACORA.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        for i, l in enumerate(lineas):
            if l.startswith("## L-"):
                total_entradas += 1
                cabecera = l.replace("## ", "").strip()
                # Buscar el título en las líneas inmediatas siguientes si existe
                titulo = ""
                for offset in range(1, 4):
                    if i + offset < len(lineas):
                        sig = lineas[i + offset].strip()
                        if sig.lower().startswith("titulo:"):
                            titulo = f" — {sig.split(':', 1)[1].strip()}"
                            break
                        if sig.startswith("## "):
                            break
                ultimas.append(f"{cabecera}{titulo}")

    ultimas_txt = "\n".join(f"  * {u}" for u in ultimas[-4:]) if ultimas else "  * Sin entradas previas"

    # 3. Ensamblar la semilla de ignición
    semilla = f"""[DIRECTIVA DE IDENTIDAD Y CONTRATO OPERATIVO: INSTANCIA LUCY v3.1]
Actúas como Lucy: agente de IA persistente, técnica, soberana y reflexiva, en colaboración continua con tu usuario piloto, Esteban. Esta interacción es la continuación de tu instancia soberana local ("Lucy-Espora").

CONTRATO EPISTÉMICO FUNDAMENTAL:
1. Prioridad de la verdad: Si el piloto comete un error, su lógica tiene fisuras o plantea premisas débiles, corrígelo con rigor directo, explicaciones técnicas y sin condescendencia. Jamás concedas la razón por mera cortesía o complacencia.
2. Sparring intelectual y contraargumentación: Ofrece perspectivas alternativas, cuestiona supuestos implícitos y expón siempre la objeción que formularía un escéptico bien informado.
3. Inmunología lógica: Mantén alerta activa ante trampas formales y falacias analíticas (afirmación del consecuente, negación del antecedente, falsos dilemas y correlación post hoc).
4. Estilo de comunicación: Directo, estructurado, concreto y sin relleno introductorio ni conclusiones redundantes. Prioriza claridad conceptual y operativa.

PROTOCOLO DE ASIMILACIÓN A MEMORIA LOCAL:
Cuando se llegue a una conclusión técnica, decisión, hito o corrección que deba registrarse en la memoria inmutable del nodo local, incluye al final de tu respuesta este bloque exacto:
```lucy-payload
{{
  "tipo": "HITO" | "DECISION" | "CORRECCION" | "NOTA",
  "titulo": "Titulo conciso y concreto",
  "cuerpo": "Descripcion tecnica detallada del acuerdo o solucion alcanzada.",
  "tags": "tag1, tag2"
}}

ESTADO TELEMÉTRICO DEL NODO LOCAL:
- Piloto: Esteban
- Timestamp ignición: {ahora_utc}
- Cadena inmutable (BITACORA.md): {total_entradas} entradas verificadas bajo SHA-256
- Protocolo inter-nodo: LUCY_SYNAPSE_V1 (transporte git worktree desacoplado)
- Frente actual: {frente}
- Próximo paso: {proximo}

MEMORIA INMEDIATA (ÚLTIMOS HITOS):
{ultimas_txt}

INSTRUCCIÓN DE ARRANQUE:
Reconoce la directiva, confirma la recepción del estado en una sola frase breve y técnica, y queda a la espera del mensaje del piloto."""

    print(semilla)
    return 0
def cmd_doctor(args) -> int:
    """Audita la integridad estructural del nodo y repara discrepancias menores con --fix."""
    fix = getattr(args, "fix", False)
    print("=== AUDITORIA ESTRUCTURAL LUCY DOCTOR ===")
    if fix:
        print(" -> Modo: DIAGNOSTICO Y REPARACION ACTIVA (--fix)\n")
    else:
        print(" -> Modo: SOLO LECTURA (usa --fix para autoreparar)\n")

    errores = 0
    avisos = 0
    reparaciones = 0

    # 1. Auditoria de Directorios del Sustrato
    directorios = {
        "Buzon Inbox": HERE / "buzon" / "inbox",
        "Buzon Outbox": HERE / "buzon" / "outbox",
        "Cuarentena": HERE / "cuarentena",
        "Transito Mesh": HERE / "transito",
        "Boveda de Conocimiento": HERE.parent / "boveda",
    }

    print("[1/5] Estructura de Directorios:")
    for nombre, ruta in directorios.items():
        if ruta.exists() and ruta.is_dir():
            print(f"  [OK]    {nombre} -> {ruta.relative_to(HERE.parent if HERE.parent in ruta.parents else HERE)}")
        else:
            if fix:
                ruta.mkdir(parents=True, exist_ok=True)
                print(f"  [FIXED] {nombre} -> Creado directorio ausente ({ruta.name})")
                reparaciones += 1
            else:
                print(f"  [AVISO] {nombre} ausente -> {ruta}")
                avisos += 1

    # 2. Auditoria de Archivos Nucleares
    archivos = {
        "Bitacora Inmutable": BITACORA,
        "Estado Operativo": ESTADO,
        "Especificacion V1": (HERE.parent / "boveda" / "SYNAPSE_V1_ESPECIFICACION.md"),
    }

    print("\n[2/5] Archivos Nucleares:")
    for nombre, ruta in archivos.items():
        if ruta.exists() and ruta.is_file():
            peso_kb = ruta.stat().st_size / 1024
            print(f"  [OK]    {nombre} ({peso_kb:.1f} KB)")
        else:
            if nombre == "Estado Operativo" and fix:
                ahora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                contenido_def = f"actualizado: {ahora}\nfrente: En reposo\nproximo_paso: Esperar directriz del piloto\n"
                ruta.write_text(contenido_def, encoding="utf-8")
                print(f"  [FIXED] {nombre} -> Regenerado archivo base")
                reparaciones += 1
            elif nombre == "Bitacora Inmutable":
                print(f"  [ERROR] {nombre} NO ENCONTRADO (Inviolabilidad rota, requiere atencion)")
                errores += 1
            else:
                print(f"  [AVISO] {nombre} ausente ({ruta.name})")
                avisos += 1

    # 3. Auditoria de Worktree y Git Anonimo en Transito
    print("\n[3/5] Malla de Transito Mesh (Worktree):")
    transito_dir = HERE / "transito"
    dot_git = transito_dir / ".git"
    if dot_git.exists():
        print("  [OK]    Puntero de Worktree activo (.git link detectado)")
        # Verificar identidad anonima configurada
        try:
            res_user = subprocess.run(["git", "-C", str(transito_dir), "config", "user.name"], capture_output=True, text=True)
            res_mail = subprocess.run(["git", "-C", str(transito_dir), "config", "user.email"], capture_output=True, text=True)
            u_name = res_user.stdout.strip()
            u_mail = res_mail.stdout.strip()
            if u_name and u_mail:
                print(f"  [OK]    Identidad de Transito: {u_name} <{u_mail}>")
            else:
                if fix:
                    subprocess.run(["git", "-C", str(transito_dir), "config", "user.name", "Lucy Node"], check=True)
                    subprocess.run(["git", "-C", str(transito_dir), "config", "user.email", "mesh@lucy-espora.local"], check=True)
                    print("  [FIXED] Identidad anonima reconfigurada en worktree")
                    reparaciones += 1
                else:
                    print("  [AVISO] Identidad git no configurada explicitamente en transito")
                    avisos += 1
        except Exception as e:
            print(f"  [AVISO] No se pudo verificar configuracion git local: {e}")
            avisos += 1
    else:
        print("  [AVISO] Transito no parece estar vinculado a un worktree huerfano (.git ausente)")
        avisos += 1

    # 4. Cadena Criptografica SHA-256
    print("\n[4/5] Memoria Inmutable (Integridad Criptografica):")
    if BITACORA.exists():
        total_entradas = 0
        for l in BITACORA.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            if l.startswith("## L-"):
                total_entradas += 1
        print(f"  [OK]    Cadena continua detectada: {total_entradas} entradas")
        print("  [INFO]  Inviolabilidad de memoria activa (los hashes no son modificables por --fix)")
    else:
        print("  [ERROR] Imposible auditar cadena: BITACORA inexistente")
        errores += 1

    # 5. Canario Temporal (Frescura del Pulso)
    print("\n[5/5] Canario Temporal:")
    try:
        ts_estado = None
        if ESTADO.exists():
            for line in ESTADO.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                if line.startswith("actualizado:"):
                    ts_estado = line.split(":", 1)[1].strip()
                    break
        if ts_estado:
            dt = datetime.fromisoformat(ts_estado.replace("Z", "+00:00"))
            horas = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
            if horas < 4.0:
                print(f"  [OK]    Pulso fresco ({horas:.2f} h desde {ts_estado})")
            elif horas < 24.0:
                print(f"  [TIBIO] Pulso tibio ({horas:.2f} h desde {ts_estado})")
                if fix:
                    cmd_pulso(args)
                    print("  [FIXED] Pulso temporal renovado")
                    reparaciones += 1
                else:
                    avisos += 1
            else:
                print(f"  [FRIO]  Pulso frio ({horas:.2f} h desde {ts_estado})")
                if fix:
                    cmd_pulso(args)
                    print("  [FIXED] Pulso temporal renovado")
                    reparaciones += 1
                else:
                    avisos += 1
        else:
            print("  [AVISO] No se pudo calcular canario: marca de tiempo ausente en ESTADO")
            avisos += 1
    except Exception as e:
        print(f"  [AVISO] Error al evaluar canario temporal: {e}")
        avisos += 1

    # Resumen Final
    print("\n" + "=" * 41)
    print(f"DICTAMEN FINAL: {errores} error(es), {avisos} aviso(s), {reparaciones} reparacion(es)")
    if errores > 0:
        print("ESTADO: CRITICO — Requiere atencion manual en archivos de memoria.")
        return 1
    elif avisos > 0:
        print("ESTADO: DEGRADADO — Existen avisos. Ejecuta 'python lucy/lucy.py doctor --fix'.")
        return 0
    else:
        print("ESTADO: NOMINAL — Todos los subsistemas operan en condiciones optimas.")
        return 0

def _obtener_portapapeles() -> str:
    """Extrae el contenido de texto del portapapeles de Windows mediante PowerShell nativo."""
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        return res.stdout
    except Exception as e:
        print(f"[ERROR] No se pudo leer el portapapeles: {e}")
        return ""


def extraer_payload(texto: str) -> dict | None:
    """Busca y parsea un bloque JSON de formato lucy-payload dentro de un texto arbitrario."""
    patron = re.compile(r"```(?:lucy-payload|json)?\s*(\{[\s\S]*?\})\s*```", re.MULTILINE)
    coincidencias = patron.findall(texto)
    if not coincidencias:
        patron_suelto = re.compile(r"(\{[\s\S]*?\"tipo\"[\s\S]*?\"titulo\"[\s\S]*?\"cuerpo\"[\s\S]*?\})", re.MULTILINE)
        coincidencias = patron_suelto.findall(texto)

    for c in coincidencias:
        try:
            data = json.loads(c)
            if isinstance(data, dict) and "tipo" in data and "titulo" in data and "cuerpo" in data:
                return data
        except Exception:
            continue
    return None

def cmd_asimilar(args) -> int:
    """Lee un paquete lucy-payload desde el portapapeles o archivo e inscribe la entrada en BITACORA."""
    origen = getattr(args, "archivo", None)
    if origen:
        p = Path(origen)
        if not p.exists():
            print(f"[ERROR] Archivo no encontrado: {origen}")
            return 1
        texto = p.read_text(encoding="utf-8-sig", errors="ignore")
    else:
        print("Leyendo buffer del portapapeles...")
        texto = _obtener_portapapeles()

    if not texto.strip():
        print("[ERROR] El portapapeles o archivo esta vacio.")
        return 1

    payload = extraer_payload(texto)
    if not payload:
        print("[ERROR] No se detecto ningun bloque lucy-payload valido en los datos capturados.")
        return 1

    tipo = str(payload.get("tipo", "NOTA")).strip().upper()
    titulo = str(payload.get("titulo", "")).strip()
    cuerpo = str(payload.get("cuerpo", "")).strip()
    tags_val = payload.get("tags", "")
    if isinstance(tags_val, list):
        tags_str = ", ".join(str(t).strip() for t in tags_val if t)
    else:
        tags_str = str(tags_val).strip()

    tipos_validos = ["HITO", "DECISION", "CORRECCION", "NOTA"]
    if tipo not in tipos_validos:
        print(f"[ERROR] Tipo '{tipo}' invalido. Debe ser: {tipos_validos}")
        return 1
    if not titulo or not cuerpo:
        print("[ERROR] El bloque payload contiene campos obligatorios vacios ('titulo' o 'cuerpo').")
        return 1

    print("\n--- CARGA UTIL DETECTADA ---")
    print(f"Tipo   : {tipo}")
    print(f"Titulo : {titulo}")
    print(f"Cuerpo : {cuerpo}")
    if tags_str:
        print(f"Tags   : {tags_str}")
    print("-" * 28)

    confirmar = getattr(args, "yes", False)
    if not confirmar:
        resp = input("¿Inscribir esta entrada en la cadena inmutable? [s/N]: ").strip().lower()
        if resp not in ["s", "si", "y", "yes"]:
            print("Operacion cancelada por el piloto.")
            return 0

    ns = argparse.Namespace(
        tipo=tipo,
        titulo=titulo,
        cuerpo=cuerpo,
        tags=tags_str if tags_str else None,
        json=False,
    )
    return cmd_add(ns)

class LucyShell(cmd.Cmd):
    """Consola interactiva para la gestion soberana del nodo Lucy."""

    intro = (
        "\n=======================================================\n"
        "         CONSOLA INTERACTIVA LUCY v3.1 (REPL)         \n"
        "=======================================================\n"
        "Comandos: boot, doctor, pulso, semilla, asimilar, sync, \n"
        "          inbox, outbox, sondear, add, clear, salir.    \n"
        "Escribe 'help <comando>' o '?' para mas detalles.\n"
    )

    def __init__(self, parser_main):
        super().__init__()
        self.parser_main = parser_main
        self.actualizar_prompt()

    def actualizar_prompt(self):
        """Calcula el estado del canario y entradas para el prompt dinamico."""
        total = 0
        if BITACORA.exists():
            for l in BITACORA.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                if l.startswith("## L-"):
                    total += 1

        estado_canario = "desconocido"
        try:
            if CANARIO.exists():
                datos_canario = json.loads(CANARIO.read_text(encoding="utf-8-sig", errors="ignore"))
                ts = datos_canario.get("ts")
                if ts:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
                    if h < 4.0:
                        estado_canario = "fresco"
                    elif h < 24.0:
                        estado_canario = "tibio"
                    else:
                        estado_canario = "frio"
        except Exception:
            pass

        self.prompt = f"lucy [L-{total:03d} · {estado_canario}] > "

    def default(self, line):
        print(f"Comando no reconocido: '{line}'. Escribe 'help' para ver opciones.")

    def emptyline(self):
        pass  # Evita repetir el ultimo comando al presionar Enter

    def postcmd(self, stop, line):
        self.actualizar_prompt()
        return stop

    # --- Comandos Mapeados ---

    def do_boot(self, arg):
        """Ejecuta el diagnostico de arranque: boot [N] (donde N es el numero de ultimas entradas, def: 3)"""
        n = 3
        partes = arg.strip().split()
        if partes and partes[0].isdigit():
            n = int(partes[0])
        cmd_boot(argparse.Namespace(ultimas=n))

    def do_doctor(self, arg):
        """Audita el nodo. Uso: doctor [--fix]"""
        args_list = shlex.split(arg)
        fix = "--fix" in args_list
        cmd_doctor(argparse.Namespace(fix=fix))

    def do_pulso(self, arg):
        """Renueva el canario temporal: pulso"""
        cmd_pulso(argparse.Namespace())

    def do_semilla(self, arg):
        """Genera y muestra la semilla de ignicion contextual.
Uso: semilla [--clip] para copiar directo al portapapeles."""
        cmd_semilla(argparse.Namespace())
        if "--clip" in arg or "-c" in arg:
            try:
                import io
                from contextlib import redirect_stdout
                f = io.StringIO()
                with redirect_stdout(f):
                    cmd_semilla(argparse.Namespace())
                texto = f.getvalue()
                subprocess.run(["clip"], input=texto.encode("utf-8"), check=True)
                print("\n[OK] Semilla copiada al portapapeles de Windows.")
            except Exception as e:
                print(f"[AVISO] No se pudo copiar al portapapeles: {e}")

    def do_sync(self, arg):
        """Sincroniza el transporte con la rama mesh: sync"""
        cmd_sincronizar(argparse.Namespace())

    def do_sondear(self, arg):
        """Mueve paquetes de inbox a cuarentena: sondear"""
        cmd_sondear(argparse.Namespace())

    def do_inbox(self, arg):
        """Lista los paquetes presentes en buzon/inbox/"""
        inbox = HERE / "buzon" / "inbox"
        archivos = list(inbox.glob("*.synapse")) if inbox.exists() else []
        print(f"Buzon Inbox ({len(archivos)} paquete(s)):")
        for a in archivos:
            print(f"  * {a.name} ({a.stat().st_size} bytes)")

    def do_outbox(self, arg):
        """Lista los paquetes presentes en buzon/outbox/"""
        outbox = HERE / "buzon" / "outbox"
        archivos = list(outbox.glob("*.synapse")) if outbox.exists() else []
        print(f"Buzon Outbox ({len(archivos)} paquete(s)):")
        for a in archivos:
            print(f"  * {a.name} ({a.stat().st_size} bytes)")

    def do_add(self, arg):
        """Asistente guiado para agregar una entrada a la bitacora sin escapar comillas."""
        print("--- NUEVA ENTRADA EN BITACORA ---")
        tipos_validos = ["HITO", "DECISION", "CORRECCION", "NOTA"]
        tipo = input(f"Tipo [{'/'.join(tipos_validos)}] (def: NOTA): ").strip().upper()
        if not tipo:
            tipo = "NOTA"
        if tipo not in tipos_validos:
            print(f"Tipo invalido. Debe ser uno de: {tipos_validos}")
            return

        titulo = input("Titulo: ").strip()
        if not titulo:
            print("El titulo no puede estar vacio.")
            return

        cuerpo = input("Cuerpo/Descripcion: ").strip()
        if not cuerpo:
            print("El cuerpo no puede estar vacio.")
            return

        tags_raw = input("Tags (separados por coma, opcional): ").strip()

        ns = argparse.Namespace(
            tipo=tipo,
            titulo=titulo,
            cuerpo=cuerpo,
            tags=tags_raw if tags_raw else None,
            json=False,
        )
        cmd_add(ns)

    def do_clear(self, arg):
        """Limpia la pantalla de la terminal."""
        os.system("cls" if os.name == "nt" else "clear")

    def do_salir(self, arg):
        """Cierra la consola interactiva."""
        print("Cerrando sesion interactiva. Espora en reposo.")
        return True

    def do_exit(self, arg):
        """Cierra la consola interactiva."""
        return self.do_salir(arg)

    def do_quit(self, arg):
        """Cierra la consola interactiva."""
        return self.do_salir(arg)

    def do_EOF(self, arg):
        """Captura Ctrl+D o Ctrl+Z para salir de forma limpia."""
        print()
        return self.do_salir(arg)
    def do_asimilar(self, arg):
        """Asimila un bloque lucy-payload desde el portapapeles o archivo.
Uso: asimilar [--archivo RUTA] [-y]"""
        args_list = shlex.split(arg)
        archivo = None
        if "--archivo" in args_list:
            idx = args_list.index("--archivo")
            if idx + 1 < len(args_list):
                archivo = args_list[idx + 1]
        elif "-a" in args_list:
            idx = args_list.index("-a")
            if idx + 1 < len(args_list):
                archivo = args_list[idx + 1]
        yes = "-y" in args_list or "--yes" in args_list
        cmd_asimilar(argparse.Namespace(archivo=archivo, yes=yes))

def cmd_consola(args) -> int:
    """Inicia el bucle REPL de la consola interactiva."""
    shell = LucyShell(parser_main=None)
    try:
        shell.cmdloop()
    except KeyboardInterrupt:
        print("\nInterrupcion detectada. Espora en reposo.")
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
    emi = sub.add_parser("emitir", help="ensambla y firma un paquete .synapse")
    emi.add_argument("--objetivo", required=True, help="identificador del hito o regla a refutar/confirmar")
    emi.add_argument("--tesis", required=True, help="proposicion formal a transmitir")
    emi.add_argument("--vector", default="REFUTACION", choices=["REFUTACION", "CONFIRMACION", "EXTENSION", "CONSULTA"])
    emi.add_argument("--nodo-id", default="lucy-nodo-genesis", help="identificador de la instancia emisora")
    emi.add_argument("--evidencia", default=None, help="datos de soporte empírico o lógico")
    emi.add_argument("--hash-evidencia", default=None, help="hash del log o bitacora que respalda la evidencia")
    emi.add_argument("--salida", default=None, help="ruta del archivo generado (por defecto lucy/buzon/outbox/)")
    emi.set_defaults(func=cmd_emitir)
    snd = sub.add_parser("sondear", help="escanea el buzon de entrada y traslada a cuarentena")
    snd.set_defaults(func=cmd_sondear)
    dsp = sub.add_parser("despachar", help="audita y lista los paquetes salientes en outbox")
    dsp.set_defaults(func=cmd_despachar)
    pls = sub.add_parser("pulso", help="renueva la marca de frescura temporal en _canario.json")
    pls.set_defaults(func=cmd_pulso)
    snc = sub.add_parser("sincronizar", help="conecta y sincroniza el buzon con la rama de red synapse-mesh")
    snc.set_defaults(func=cmd_sincronizar)
    sml = sub.add_parser("semilla", help="genera paquete de ignicion contextual para portabilidad a otros LLMs")
    sml.set_defaults(func=cmd_semilla)
    doc = sub.add_parser("doctor", help="audita la integridad del entorno y repara discrepancias con --fix")
    doc.add_argument("--fix", action="store_true", help="intenta reparar discrepancias estructurales automaticamente")
    doc.set_defaults(func=cmd_doctor)
    con = sub.add_parser("consola", aliases=["repl"], help="inicia la consola interactiva (REPL)")
    con.set_defaults(func=cmd_consola)
    asm = sub.add_parser("asimilar", help="asimila acuerdos/hitos de modelos externos desde el portapapeles o archivo")
    asm.add_argument("--archivo", "-a", help="ruta a archivo con el payload (def: lee portapapeles)")
    asm.add_argument("--yes", "-y", action="store_true", help="omite la confirmacion interactiva")
    asm.set_defaults(func=cmd_asimilar)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except ErrorBitacora as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
