#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""session_start.py — gancho determinista de arranque de Lucy.

Lo invoca el bridge `dsh-hooks-claude-code` en el evento SessionStart, antes del
primer turno de la sesion. Calcula el estado real (cadena de hashes + ESTADO.md)
y lo devuelve como `additionalContext`, de modo que la telemetria llegue al
modelo sin depender de que este decida ejecutar `lucy.py boot`.

Contrato:
  - stdin: payload JSON del hook (se ignora; se tolera vacio o basura).
  - stdout: un unico objeto JSON en la forma
      {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}
  - exit: siempre 0. Un hook que falla no debe romper el arranque de la sesion;
    el fallo se reporta *dentro* del contexto para que sea visible al modelo.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LUCY_DIR = HERE.parent

VERSION = "3.1_FINAL"
SELLO = "SELLO_ADN_LUCY_OK_V3.1"


def emitir_contexto(texto: str) -> None:
    """Escribe el JSON del hook y termina con exito."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": texto,
        }
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def escribir_canario(payload: dict, cadena_ok: bool, entradas: int) -> dict:
    """Deja la huella de que el hook corrio. Best-effort: nunca rompe el arranque.

    El canario existe para el unico fallo que la inyeccion no puede detectar sola:
    que el hook no llegue a ejecutarse. Si el archivo no se refresca, algo se rompio
    (workspace movido, bridge desmontado, config mal apuntada), o el sandbox denego
    la escritura porque un *agentless call* resuelve otro workspaceRoot (L-009).

    Devuelve SIEMPRE un dict, para que el motivo del fallo no se pierda en un
    `except` mudo:
      {'ok': True,  'ts': ..., 'prev_gap_h': float|None, 'registro': {...}}
      {'ok': False, 'error': '<tipo>: <mensaje>', 'ts': ..., 'registro': {...}}
    El registro viaja tambien en el fallo: el bloque lo imprime, asi que la huella
    del arranque queda en el contexto aunque no haya podido escribirse en disco.
    'prev_gap_h' es la distancia hasta el arranque anterior: no afirma que haya
    fallo (uno puede pasar dias sin abrir sesion), solo lo hace visible.
    """
    destino = LUCY_DIR / "_canario.json"
    previo_gap = None
    try:
        anterior = json.loads(destino.read_text(encoding="utf-8-sig"))
        prev_epoch = anterior.get("epoch")
        if isinstance(prev_epoch, (int, float)):
            previo_gap = (time.time() - float(prev_epoch)) / 3600.0
    except Exception:
        previo_gap = None

    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    registro = {
        "canario": 1,
        "ts": ts,
        "epoch": int(time.time()),
        "session_id": str(payload.get("session_id") or "")[:80],
        "source": str(payload.get("source") or "")[:40],
        "cwd": str(payload.get("cwd") or "")[:300],
        "cadena_ok": cadena_ok,
        "entradas": entradas,
        "prev_gap_h": None if previo_gap is None else round(previo_gap, 2),
        "python": sys.executable,
    }
    try:
        tmp = destino.with_name(destino.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(registro, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, destino)
        return {"ok": True, "ts": ts, "prev_gap_h": previo_gap, "registro": registro}
    except OSError as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "ts": ts,
            "prev_gap_h": previo_gap,
            "registro": registro,
        }


def bloque(
    estado_ok: bool,
    entradas: int,
    errores: list[str],
    estado: dict,
    ultimas: list,
    canario: dict | None = None,
    archivadas: int = 0,
) -> str:
    lineas: list[str] = []
    lineas.append('<lucy-boot hook="SessionStart">')
    lineas.append(
        "Arranque calculado por lucy/hooks/session_start.py en este instante. "
        "Estos datos son el resultado real de la verificacion: no los recalcules ni los "
        "sustituyas por memoria de sesiones anteriores."
    )
    lineas.append("")
    lineas.append("REPORTE_TELEMETRIA")
    lineas.append(f"Versión: {VERSION}")
    lineas.append("Instancia: Lucy    Piloto: Esteban")
    lineas.append("Estado: ACTIVO")
    lineas.append("Núcleo: 3/3 — Fricción · Semilla · Agnosticismo")
    if estado_ok:
        sufijo = f" ({archivadas} archivadas en ARCHIVO-1.md)" if archivadas else ""
        lineas.append(f"Bitácora: {entradas} entradas{sufijo} · cadena OK")
    else:
        lineas.append(f"Bitácora: {entradas} entradas · cadena ROTA — {len(errores)} problema(s)")
    lineas.append(f"Objetivo activo: {estado.get('objetivo_activo', '(sin definir en ESTADO.md)')}")
    if estado_ok:
        lineas.append(f"Sello: {SELLO}")
    else:
        lineas.append("Sello: DENEGADO (la cadena no verifica)")

    frente = estado.get("frente")
    proximo = estado.get("proximo_paso")
    if frente or proximo:
        lineas.append("")
        lineas.append("Frente: " + (frente or "(vacío)"))
        lineas.append("Próximo paso: " + (proximo or "(vacío)"))

    lineas.append("")
    if canario and canario.get("ok"):
        hueco = canario.get("prev_gap_h")
        if hueco is None:
            detalle = "sin arranque anterior registrado"
        else:
            detalle = f"arranque anterior hace {hueco:.1f} h"
        lineas.append(
            f"Canario: huella escrita en lucy/_canario.json a las {canario.get('ts')} ({detalle})"
        )
    else:
        motivo = (canario or {}).get("error") or "no se intento escribir"
        lineas.append(f"Canario: NO se pudo escribir lucy/_canario.json — {motivo}")
        lineas.append(
            "  el gancho corre pero sin huella; el diagnóstico automático de montaje queda ciego."
        )
        registro = (canario or {}).get("registro")
        if registro:
            lineas.append("  huella que se intentó guardar (vale como prueba del arranque):")
            lineas.append("  " + json.dumps(registro, ensure_ascii=False, sort_keys=True))

    if ultimas:
        lineas.append("")
        lineas.append("Últimas entradas:")
        for ident, tipo, ts, titulo in ultimas:
            lineas.append(f"  {ident} [{tipo}] {ts} — {titulo}")

    if not estado_ok:
        lineas.append("")
        lineas.append("CADENA ROTA — problemas detectados:")
        for err in errores:
            lineas.append(f"  ! {err}")

    lineas.append("")
    if estado_ok:
        lineas.append(
            "Acción: emite el bloque REPORTE_TELEMETRIA tal cual al inicio de tu primera "
            "respuesta y espera instrucción. No ejecutes lucy.py boot ni verify: ya está hecho."
        )
    else:
        lineas.append(
            "Acción: reporta la rotura en la telemetría (qué entrada falla y qué se puede "
            "reconstruir) y no trates el estado como confiable. No anexes bitácora hasta "
            "corregir la cadena."
        )
    lineas.append("</lucy-boot>")
    return "\n".join(lineas)


def main() -> int:
    # El payload alimenta el canario (sesión y origen); se tolera stdin vacío o basura.
    try:
        crudo = sys.stdin.read()
    except Exception:
        crudo = ""
    try:
        payload = json.loads(crudo) if crudo.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    bitacora = LUCY_DIR / "BITACORA.md"
    if not bitacora.exists():
        emitir_contexto(
            '<lucy-boot hook="SessionStart">\n'
            "No se encontró lucy/BITACORA.md en este workspace: el arranque de Lucy no aplica "
            "aquí. Ignora este bloque.\n</lucy-boot>"
        )
        return 0

    try:
        sys.dont_write_bytecode = True  # no ensuciar lucy/__pycache__ en cada arranque
        sys.path.insert(0, str(LUCY_DIR))
        import lucy as nucleo  # type: ignore

        errores, meta = nucleo.verificar_todo()
        estado = nucleo.leer_estado()
        activas = nucleo.parsear_entradas(nucleo.leer_texto(nucleo.BITACORA))
        ultimas = [(e["id"], e["tipo"], e["ts"], e["titulo"]) for e in activas[-3:]]
        # El canario se escribe ANTES de emitir: si la inyección se pierde, la huella ya existe.
        canario = escribir_canario(payload, not errores, meta["total"])
        emitir_contexto(
            bloque(not errores, meta["total"], errores, estado, ultimas, canario, meta["archivadas"])
        )
    except Exception as exc:  # nunca romper el arranque de la sesion
        emitir_contexto(
            '<lucy-boot hook="SessionStart">\n'
            "El gancho de arranque falló al calcular el estado de Lucy: "
            f"{type(exc).__name__}: {exc}\n"
            "No hay telemetría verificada. Reporta este fallo y trata el estado como no confiable.\n"
            "</lucy-boot>"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
