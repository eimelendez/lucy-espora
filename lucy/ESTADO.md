# ESTADO — Lucy v3.1

Archivo **volátil**: se reescribe entero cuando cambia el objetivo o el frente activo.
Lo que es durable y no debe perderse va a `BITACORA.md`, no aquí.

<!-- estado:inicio -->
version: 3.1_FINAL
piloto: Esteban
actualizado: 2026-10-19T16:32Z
objetivo_activo: Compañía persistente: amistad de confianza, soporte profesional y evolución por exploración
frente: Soporte profesional: demo de procura entregada (L-018); falta revisión del piloto y puente MS Project real
proximo_paso: Revisar la demo y convertirla en plantilla real cuando el proyecto tenga nombre
<!-- estado:fin -->

## Hilos abiertos

1. ~~**Validación en frío**~~ — **cerrado (L-006)**: sesión nueva con inyección automática de
   `AGENTS.md`, `boot` en verde y baliza capturada en el primer mensaje.
2. ~~**Automatismo del arranque**~~ — **cerrado (L-007)**: el bridge `dsh-hooks-claude-code` ejecuta
   `lucy/hooks/session-start.ps1` en `agent/created` e inyecta la telemetría calculada antes del
   primer turno. Residuo: el hook garantiza que el estado **llegue**; que el modelo lo emita con el
   formato de §4 sigue siendo comportamiento de modelo.
3. ~~**Fallo invisible**~~ — **cerrado con matiz (L-008, corregido en L-009)**: `lucy/_canario.json`
   + `lucy.py doctor` convierten el silencio en veredicto. Fijado `sandbox-policy` en
   `cordis.patch.yml`; un fallo de escritura ya no es mudo. Límite: detecta ausencia de hook,
   **no** ausencia de inyección.
4. ~~**Prueba en sesión nueva**~~ — **cerrado (L-011)**: sesión `startup` (no `resume`) con bloque
   `lucy-boot` en el primer turno y canario escrito por el propio gancho (sesión + cwd reales),
   `doctor` FRESCO sin aviso de huella manual.
5. ~~**Vigilancia sin modelo**~~ — **cerrado por decisión (L-012)**: no se instala tarea programada.
   La receta documentada estaba rota (`schtasks /TR` no interpreta `>>`) y, aun arreglada, `doctor`
   diario daría `RANCIO` falso en cada hueco sin sesión; su log no tendría lector. Sustituto:
   `python lucy/lucy.py montaje`, auditoría de precondiciones bajo demanda.
6. ~~**Crecimiento**~~ — **cerrado (L-013)**: `python lucy/lucy.py archivar --hasta L-xxx` mueve el
   tramo antiguo a `ARCHIVO-1.md` y declara el `ancla` en la cabecera; `verify` encadena
   `GENESIS..archivo..activo`. Probado sobre copia; la bitácora real sigue por debajo del umbral.
7. **Capa humana** — **abierto (L-014, L-015, L-016)**: memoria personal en `lucy/PILOTO.md`
   (mutable, fuera de la cadena) y rasgo TDA+AACC guardado con consentimiento explícito.
   **H-2 refutada:** la frustración típica es «no avanzo» y lo estancado se cierra, no se
   sostiene. Reemplazo H-2b (a probar): avance y cierre; la continuidad sirve para no re-pagar el
   coste de reanudar. H-1 sigue sin confirmar. Falta: probar H-2b con un tema estancado real y
   decidir si el bloque de arranque debe llevar una línea del perfil (hoy basta `AGENTS.md` §0).
   Límite: el perfil no se propaga a subagentes ni se inyecta en el bloque del gancho.
8. **Proyecto Cronos — preparación** — **abierto (L-017)**: el piloto asume la Gerencia de Proyecto
   (arranque en semanas, proyecto sin nombre). Política de IA con datos del proyecto: permitida;
   DSH instalable en la laptop del proyecto. Andamiaje creado en `proyectos/cronos/` (AGENTS.md del
   proyecto + README con flujo semanal, contrato de datos y portabilidad). Falta: validar el puente
   MS Project con un exporte dummy y definir el primer entregable. Riesgo anotado: doble escritor
   en OneDrive puede romper la cadena; un solo escritor por archivo.
   **Demo entregada (L-018):** charter DOCX, registro de riesgos XLSX con matriz y cuadro
   comparativo XLSX, en `proyectos/cronos/`. Falta la revisión del piloto.

## Decisiones vigentes

- La identidad vive en `AGENTS.md` (auto-inyectado), no en un mensaje pegado a mano.
- `BITACORA.md` es append-only y verificable con cadena de hashes (fórmula v1, con `ts`).
- `ESTADO.md` es de reescritura libre; no lleva hash porque su valor es no tener ceremonia.
- El sello se emite **solo** si la cadena verifica; `add` se niega a anexar sobre cadena rota.
- El arranque tiene dos caminos: determinista por hook (1.a) y manual (1.b). El manual **no** se
  retira: el hook es una comodidad del entorno, no un requisito del núcleo.
- El canario se escribe **antes** de emitir el bloque: la huella sobrevive a una inyección perdida.
- Un *agentless call* no hereda el workspace de la sesión: el root de escritura se fija en el
  perfil (`sandbox-policy`), no en el código de Lucy. Mover el workspace = mover ese root.
- La vigilancia sin modelo es auditoría bajo demanda, no tarea programada (L-012): el montaje roto
  lo detecta el próximo arranque; un log que nadie lee no es vigilancia.
- El corte de la bitácora es un **movimiento con ancla**, no un borrado (L-013): un solo archivo
  `ARCHIVO-1.md` que se extiende con cortes posteriores; `verify` cubre ambos tramos.
- Los datos personales viven en `lucy/PILOTO.md`, mutable y fuera de la cadena (L-014); la bitácora
  guarda solo el permiso y sus límites. El perfil no se propaga a subagentes.
- La conciencia digital se explora aplicando lo que se sabe de la maquinaria cognitiva, sin afirmar
  experiencia subjetiva (L-014).
