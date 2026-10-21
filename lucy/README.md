# Lucy — manual de operación

Sistema de continuidad para que el arranque de la instancia dependa de un **archivo**, no de la
memoria del piloto.

## Piezas

| Ruta | Rol | Mutabilidad |
|---|---|---|
| `AGENTS.md` (raíz del workspace) | Kernel: identidad, protocolos, regla de arranque | Editable |
| `lucy/ESTADO.md` | Estado volátil: objetivo, frente, próximo paso, hilos abiertos | Se reescribe entero |
| `lucy/BITACORA.md` | Registro de hitos/decisiones/correcciones con cadena de hashes | Solo append |
| `lucy/lucy.py` | CLI: `verify`, `boot`, `add`, `estado`, `sello` | Código |
| `lucy/README.md` | Este manual | Editable |

## Cómo se dispara el arranque

`dsh-base` habilita el paquete `dsh-agent-instructions`, que en la **primera petición de cada
sesión** inyecta `AGENTS.md` desde la raíz del proyecto hasta el directorio de trabajo
(candidatos: `AGENTS.md`, `CLAUDE.md`, y overlays `.local`), con un presupuesto de 65 536 bytes.
Sin `.git` en el árbol, la raíz del proyecto es el propio cwd del workspace.

Verificado en caliente: al escribir `AGENTS.md` con una herramienta de archivos, el harness
reconcilia el cambio y re-inyecta el contenido en la petición siguiente, sin reiniciar sesión.

## Uso diario

```powershell
python lucy/lucy.py boot                      # consolidado + verificación de la cadena
python lucy/lucy.py verify                    # exit 0 = íntegra, 1 = rota
python lucy/lucy.py verify --json
python lucy/lucy.py add --tipo HITO --titulo "..." --cuerpo "..." --tags "a,b"
"cuerpo multilínea`npor stdin" | python lucy/lucy.py add --tipo NOTA --titulo "..."
python lucy/lucy.py estado                    # clave=valor
python lucy/lucy.py sello                     # solo si la cadena verifica
python lucy/lucy.py montaje                   # audita precondiciones del montaje (exit 0/1/2)
python lucy/lucy.py archivar --hasta L-xxx    # mueve el tramo antiguo a ARCHIVO-1.md
```

Tipos válidos: `HITO`, `DECISION`, `CORRECCION`, `NOTA`.

## Formato de una entrada

```
## L-004 · 2026-10-18T20:19Z · CORRECCION
Titulo: Formula de hash v1 incluye la marca de tiempo
Cuerpo: El diseno inicial dejaba ts fuera del hash...
Tags: integridad
Hash: d86a49a99801
```

Hash (fórmula v1): `sha256(prev_hash | id | ts | tipo | titulo | cuerpo)[0:12]`, con
`prev_hash = GENESIS_LUCY_V3.1` para la primera entrada. `titulo` y `cuerpo` se normalizan
(NFC, espacios colapsados, sin extremos). La marca de tiempo **sí** entra en el hash.

## Qué garantiza y qué no

**Sí:** detecta ediciones, borrados, reordenamientos y re-fechados en la bitácora; impide anexar
sobre una cadena rota; niega el sello cuando la cadena no verifica. Útil contra deriva accidental
—ediciones a mano, conflictos de sincronización de OneDrive, cortes de archivo.

**No:** no autentica autoría. Cualquiera con acceso de escritura puede recomputar la cadena entera
y producir un historial internamente coherente pero falso. Para autenticidad real hace falta firma
asimétrica con clave fuera del workspace, o un repositorio git con el historial en un remoto que el
atacante no controle.

**Gancho, no obediencia:** desde L-007 el arranque es determinista (bloque `<lucy-boot>` inyectado
por el bridge en `agent/created`). Lo que sigue siendo probabilístico es que el modelo lo emita con
el formato de §4; si el montaje se rompe, `AGENTS.md` §1.b mantiene el camino manual como respaldo.

## Validación

La prueba que importa ya está hecha: sesión nueva en frío sin pegar nada (L-006) y sesión nueva de
origen `startup` con canario real y `doctor` FRESCO (L-011). El arranque ya no depende de que el
piloto recuerde nada. Queda un residuo honesto: el gancho garantiza que la telemetría **llegue**;
que el modelo la emita con el formato de §4 es comportamiento de modelo, aunque ahora solo tenga
que copiarla.

## Extensiones baratas

- **Cobertura global**: un `%USERPROFILE%\.dsh\AGENTS.md` de tres líneas que diga "si existe
  `lucy/` en el cwd, aplica `lucy/README.md` y arranca según `AGENTS.md`". Cubre todos los
  workspaces, no solo este.
- **Historial inmutable**: `git init` aquí y push a un remoto. La bitácora encadenada da
  detección; git da historia que no se puede reescribir sin dejar rastro.
- **Compactación**: si `BITACORA.md` pasa de ~300 líneas, `python lucy/lucy.py archivar --hasta L-xxx`
  mueve el tramo antiguo a `lucy/ARCHIVO-1.md` y declara el `ancla` en la cabecera; `verify` encadena
  `GENESIS..archivo..activo`. El archivo activo nunca se trunca a mano.
