# lucy/hooks — gancho determinista de arranque

Hace que la telemetría de arranque llegue al modelo **por construcción**, no por obediencia.
Los archivos de esta carpeta se montan en el harness y se ejecutan antes del primer turno de
cada sesión.

## Piezas

| Archivo | Rol |
|---|---|
| `session_start.py` | Lógica real: lee `BITACORA.md` y `ESTADO.md`, verifica la cadena y devuelve el bloque `<lucy-boot>` como `additionalContext`. Nunca falla con exit ≠ 0. Escribe el canario antes de emitir. |
| `session-start.ps1` | Envoltorio Windows: localiza Python (`LUCY_PYTHON` → PATH → runtime de DSH → `py -3`) y delega. Sale en silencio si el workspace no tiene `lucy/lucy.py`. |
| `hooks.json` | Config en formato Claude Code que lee el bridge: un hook `SessionStart` → `session-start.ps1`. |
| `../_canario.json` | **Generado.** Huella del último arranque: `ts`, `epoch`, `session_id`, `source`, `cadena_ok`, `entradas`, `prev_gap_h`, intérprete. Se reescribe en cada sesión; no se versiona a mano. |

## Canario: cómo se detecta que el hook dejó de correr

El fallo peligroso no es una excepción: es el silencio. Si el workspace se mueve, si la ruta
absoluta de `configPath` deja de resolver o si el bridge no monta, el efecto es idéntico a
"todo bien": no hay bloque, no hay error. El canario convierte ese silencio en una pregunta
contestable.

Cada arranque escribe `lucy/_canario.json`. Y `lucy/lucy.py doctor` lo interpreta en tres
estados sin ambigüedad:

| Estado | Significado | Exit |
|---|---|---|
| `FRESCO` | El hook corrió dentro de la ventana (`--max-age-h`, por defecto 24 h). | 0 |
| `RANCIO` | Corrió, pero hace más de la ventana. Si acabas de abrir sesión: montaje roto. | 1 |
| `SIN REGISTRO` | No existe la huella: el hook no ha corrido nunca aquí. | 2 |
| — | Cadena rota (independiente del canario). | 3 |

```powershell
python lucy/lucy.py doctor            # veredicto legible
python lucy/lucy.py doctor --json     # para automatizar
python lucy/lucy.py doctor --max-age-h 72
```

El contraste que importa está en `AGENTS.md` §1.b: **un canario `FRESCO` combinado con la
ausencia del bloque es un fallo de inyección**, no normalidad. `RANCIO` o `SIN REGISTRO` es
un fallo de montaje. El bloque inyectado también incluye la línea `Canario:` con el hueco
desde el arranque anterior: deja la evidencia en la conversación, no solo en disco.

**Vigilancia sin tarea (decisión L-012).** No hay tarea programada, y no por pereza: `doctor`
a diario daría `RANCIO` en los huecos en que el piloto no abre sesión —el caso normal, no el
fallo— y su log no lo leería nadie. Un vigilante que grita sin testigo no es vigilancia.

Lo que sí existe es auditoría de precondiciones bajo demanda:

```powershell
python lucy/lucy.py montaje           # exit 0 OK · 1 problemas · 2 sin perfil (incompleta)
python lucy/lucy.py montaje --json
python lucy/lucy.py montaje --perfil <ruta a cordis.patch.yml>
```

Comprueba: archivos del núcleo, cadena de hashes, claves de `ESTADO.md`, canario legible (la edad
se reporta como dato, nunca como veredicto) y `cordis.patch.yml` — entrada `hooks-claude-code` con
`configPath` que resuelve, y `sandbox-policy` con `workspaceRoot` = workspace y
`mode: workspace-write`. No demuestra que el gancho corra: eso solo lo prueba una sesión real.

La receta anterior con `schtasks /TR "... >> _doctor.log 2>&1"` no redirigía nada: el Programador
de tareas usa `CreateProcess`, que no interpreta redirecciones, y `doctor` recibía `>>` como
argumento. Queda retirada; si alguien vuelve a querer una tarea, que sea con un `.ps1` envoltorio
y con un lector real del log, no con la ilusión de uno.

## Cómo se monta (DSH)

El harness expone los hooks de Claude Code mediante el paquete `@deepseek-ai/dsh-hooks-claude-code`
(bridge que engancha `agent/created` y ejecuta el comando con el cwd de la sesión). No viene
montado por defecto: se activa en el perfil del escritorio.

En `~/.dsh/profiles/desktop/cordis.patch.yml` (capa de usuario del perfil):

```yaml
- insert:
    - id: hooks-claude-code
      name: '@deepseek-ai/dsh-hooks-claude-code'
      config:
        configPath: C:/.../default-workspace/lucy/hooks/hooks.json
        defaultTimeoutMs: 20000
```

Notas de despliegue:

- El perfil `desktop` lo gestiona la app Electron: el CLI `dsh` se niega a tocarlo. Se edita el
  archivo a mano (es su capa de usuario) y la app recarga la config en caliente.
- `configPath` debe ser **absoluto**: se resuelve desde el directorio de lanzamiento del proceso,
  no desde el workspace.
- Una sola config por proceso: el bridge lee `hooks.json` al arrancar, así que esta entrada es
  global para todos los workspaces. La guarda del `.ps1` (¿existe `lucy/lucy.py`?) evita ruido
  fuera de este workspace.

## Desmontar

Borra el bloque `- insert:` de arriba (o restaura `cordis.patch.yml.bak-*`) y reinicia la app.
El arranque vuelve al camino manual de `AGENTS.md` §1.b sin más cambios: el núcleo no depende del hook.

## Verificar que funciona

1. En una sesión nueva, el primer mensaje del modelo debe citar el bloque `<lucy-boot>` sin haber
   ejecutado `lucy.py`.
2. Prueba directa del comando, tal como lo lanza el ejecutor:

```powershell
$env:CLAUDE_PROJECT_DIR = (Get-Location).Path
'{"hook_event_name":"SessionStart","source":"startup"}' |
  pwsh -NoLogo -NoProfile -NonInteractive -Command "& '.\lucy\hooks\session-start.ps1'"
```

Debe imprimir un único JSON con `hookSpecificOutput.hookEventName = "SessionStart"`.

3. Rama de fallo: copia `lucy/` a `lucy/_pruebas/rota/`, altera un `Cuerpo:` y ejecuta
   `session_start.py` desde esa copia. Debe reportar `cadena ROTA` y `Sello: DENEGADO`
   sin anexar nada.

## Límites conocidos

- **El hook inyecta, no decide.** Garantiza que el estado llegue; que el modelo lo emita con el
  formato de §4 sigue siendo comportamiento del modelo (aunque ahora solo tenga que copiar).
- **El canario detecta ausencia de hook, no ausencia de inyección.** Si el hook corre y el bloque
  se pierde antes del contexto, el archivo está fresco y el sistema no ve nada raro: el contraste
  lo tiene que hacer quien mira (§1.b) o el piloto. Es detección, no corrección.
- **El hook corre como *agentless call***: `runHook` no le pasa la política de la sesión, así que el
  sandbox resuelve el `workspaceRoot` del perfil (`sandbox-policy` en `cordis.patch.yml`), no el cwd
  de la sesión. Si ese root no apunta al workspace, el hook lee sin problema y **no escribe** el
  canario. Si el workspace se mueve, ese root hay que moverlo con él (L-009).
- **También dispara en subagentes** (comparten `agent/created`): ~1,3 KB de contexto por subagente,
  y el canario se refresca también con ellos.
- **Sin sesión no hay detección de ejecución.** `montaje` audita la configuración; que el gancho
  haya corrido solo lo prueba un arranque real. Por eso la vigilancia sin modelo no es una tarea
  diaria: sería un log sin lector (L-012).
- **Depende de pwsh y Python.** Si falta cualquiera de los dos, el gancho emite un aviso visible
  en el contexto en vez de morir en silencio.
- **El bridge es un adaptador de compatibilidad**, no un motor propio: no soporta más eventos
  de Claude Code que los siete documentados por el paquete.
