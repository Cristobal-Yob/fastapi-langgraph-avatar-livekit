# LiveKit, Tavus y ElevenLabs

Proyecto autónomo con su propio router, responder y fallback LangGraph. No necesita otro repositorio ni una API central. Estado: fuentes preparadas, **no desplegado**. No se han probado cuentas ni proveedores reales en esta variante cloud.

## Comenzar desde cero

1. Instale Python 3.12 y cree un entorno nuevo: `python -m venv .venv`.
2. Actívelo: Windows `.venv\Scripts\Activate.ps1`; Linux/macOS `source .venv/bin/activate`.
3. Instale `python -m pip install -r requirements-dev.txt`.
4. Copie `.env.example` a `.env`, genere sus propias claves y complete únicamente su configuración.
5. Ejecute `python -B -m unittest discover -s tests -v` y siga [la preparación específica](HEROKU.md#preparación-específica).
6. Inicie `python -B -m cloud.run`. La comprobación local es `http://localhost:8000/health`.

La clave base se genera con `python -c "import secrets; print(secrets.token_urlsafe(32))"`. No use contraseñas humanas ni copie claves de otro ejemplo. El servidor rechaza secretos débiles y no imprime contenido de conversaciones ni errores de proveedores.

## Contrato y límites

- Un único operador, un proceso Python y un dyno. No es una aplicación multiusuario ni multiworker.
- `POST /chat` autenticado devuelve 202 y `status_url`; consulte esa URL con el mismo bearer hasta `done` o `failed`. `request_id` es obligatorio. Un identificador con distinto contenido devuelve 409. Los adaptadores incluidos hacen esta consulta sin reenviar el mensaje.
- El trabajo asincrónico conserva ambas etapas del grafo y el fallback de cada una. Las peticiones HTTP individuales son cortas; no se eleva un timeout para eludir los 30 segundos del router Heroku.
- Conversaciones, resultados y colas son RAM limitada: 64 conversaciones, 6 turnos, 128 trabajos por proceso y 8 trabajos activos. Se pierden al reiniciar. Un trabajo perdido no autoriza repetir efectos.
- Las reservas de correo, credenciales WhatsApp y prueba pagada requieren PostgreSQL externo y claves estables. Un resultado incierto nunca se repite automáticamente. No se guardan audios ni transcripciones en disco.
- `/health` solo confirma proceso HTTP, no credenciales, conexión WhatsApp ni entrega. Pruebas sintéticas no demuestran envío, recepción humana ni reproducción audiovisual real.

## Siguiente paso

[Guía Heroku desde cero](HEROKU.md) · [Procedencia y cambios](SOURCE.md)

Commit sugerido cuando el propietario lo autorice: `feat(avatar-livekit): add autonomous LangGraph Heroku teaching app`.
