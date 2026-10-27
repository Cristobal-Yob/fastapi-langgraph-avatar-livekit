# Publicar LiveKit, Tavus y ElevenLabs desde cero

**Objetivo: Heroku Cedar con buildpacks clásicos y stack heroku-24. No se afirma compatibilidad Fir/CNB.** Las órdenes de esta guía son acciones futuras del propietario: no se ha creado ninguna app, add-on ni despliegue.

## 1. Preparar herramientas y cuentas

Necesita Python 3.12, Git, GitHub CLI, Heroku CLI y cuentas propias. Este proyecto es autónomo y debe publicarse en un repositorio GitHub **privado nuevo**, sin importar historial, entornos, `.env`, OAuth ni auth de ejemplos anteriores. Consulte [README](README.md) para entorno y pruebas.

## 2. Configurar el agente

Copie `.env.example` a `.env` solo localmente. Genere `AGENT_API_TOKEN` con `secrets.token_urlsafe(32)`. Elija Google Developer API con `GOOGLE_GENAI_USE_VERTEXAI=false` y una clave propia, o Vertex con proyecto/location y ADC configurado expresamente fuera de Git. No use variables/cuentas de otra aplicación. `MODELO` y `FALLBACK_MODEL` deben ser distintos. Las instrucciones y el fallback son los del agente fuente, sin un responder de demostración.

`PUBLIC_ORIGIN` debe ser la URL HTTPS exacta que obtenga para su app, sin slash final. La excepción de ejecución local es la API base en localhost; los controles de navegador seguros requieren HTTPS local con un proxy de confianza o un túnel **solo si usted lo autoriza**. No desactive la validación de Origin para probarlos.

## Preparación específica

El flujo conectado es el SDK LiveKit real, Tavus Echo para vídeo y ElevenLabs para STT/TTS. El adaptador LLM llama y consulta trabajos de **su propio** LangGraph por loopback. No necesita ngrok ni callback público. Abra la página HTTPS, desbloquee con el bearer y use los controles de consentimiento, inicio, unión, micrófono y finalización.

Mantenga `AVATAR_ENABLE_PAID=false` y `AVATAR_ISOLATION_CONFIRMED=false` hasta una autorización expresa para usar créditos. Configure PostgreSQL, `STATE_NAMESPACE` y clave Fernet estable, además de `LIVEKIT_URL` (wss de su proyecto LiveKit Cloud), sus claves API, `TAVUS_API_KEY`, `TAVUS_FACE_ID`, `ELEVENLABS_API_KEY` y `ELEVENLABS_VOICE_ID`. Verifique que ese proyecto LiveKit no tenga workers/autodispatch externos y que la cuenta/proyecto de Tavus tenga el modo Echo permitido. Las claves no llegan al navegador; solo recibe tokens breves limitados a la sala propia.

El presupuesto es una prueba de hasta `AVATAR_MAX_SECONDS=100`, máximo ocho intentos del core, una única reserva duradera antes de crear recursos, una sesión/propietario bajo bloqueo PostgreSQL y sin grabación. Iniciar la página no crea nada. No cambie namespace ni borre la reserva para repetir una prueba. Un reinicio solo reconcilia/limpia IDs propios conocidos; una creación incierta exige conciliación manual y nunca se repite. Finalizar responde rápido y la limpieza sigue en segundo plano; revise `cleanup_confirmed` y los proveedores antes de afirmar cierre.

Heroku concede 30 segundos de apagado, insuficientes para prometer continuidad LiveKit: un deploy/reinicio puede interrumpir audio y vídeo. Hay watchdog y limpieza acotada, pero no garantía de cierre remoto si el proceso termina forzosamente. Audio y transcripciones quedan solo en RAM local. Las pruebas sintéticas del SDK no acreditan una conversación real ni que alguien haya visto u oído al avatar.

## 3. Verificar antes de publicar

```sh
python -B -m unittest discover -s tests -v
```

En WhatsApp añada `npm test`. Los tests de PostgreSQL se habilitan únicamente contra una base **sintética aislada** con `RUN_DATABASE_TESTS=true`, `DATABASE_URL` y `STATE_ENCRYPTION_KEY` de prueba. Los tests no deben tocar la cuenta/base real. El resultado omitido no equivale a una prueba de durabilidad.

## 4. Crear repositorio privado y app, con aprobación

Revise primero el contenido: solo código, tests, manifiestos y estas guías. No publique `.env`, claves, QR, sesiones, ledgers, uploads, entornos ni node_modules. Inicialice Git **dentro de esta carpeta nueva**, añada el allowlist revisado, haga un commit convencional y cree el repositorio privado mediante `gh repo create <su-organizacion>/<nombre> --private --source=. --remote=origin`. Empuje solo ese historial limpio tras su aprobación. No copie el `.git` original.

Cree explícitamente una aplicación **Cedar** en Heroku con stack `heroku-24`, no Fir. Asigne `heroku/python` como buildpack clásico. Su `Procfile` raíz ejecuta `web: python -B -m cloud.run`; no es un comando de desarrollo ni usa reload. `$PORT` lo lee el supervisor y el proceso público escucha en `0.0.0.0:$PORT`.

Complete las Config Vars desde un canal privado seguro, no desde un archivo versionado ni comandos que queden en capturas. Use la URL de la app en `PUBLIC_ORIGIN`. Las dependencias están fijadas en los manifiestos. No hay add-ons automáticos: use únicamente un PostgreSQL externo previamente autorizado cuando corresponda. Verifique TLS y acceso restringido de esa base con su proveedor.

Solo después de revisar configuración, alcance y coste, conecte la app al repositorio privado y despliegue la rama elegida, o añada el remoto Heroku y haga el push manual aprobado. Escale a **un solo dyno web** y un solo proceso del core. No habilite autoscaling, workers extra ni despliegues automáticos antes de validar seguridad y presupuesto. El filesystem del dyno es efímero; no use SQLite para estado de seguridad.

## 5. Aceptación y operación

1. Consulte `/health`: debe responder sin datos privados. Verifique que una solicitud de chat/control sin autenticación sea rechazada.
2. Pruebe primero una consulta inocua con su propio operador. El cliente debe recibir 202 y consultar estado; no mantenga una petición bloqueada más de 30 segundos.
3. Solo si está autorizado, habilite por separado correo, pairing o sesión pagada y pruebe con destinatario/cuenta propios. Registre aceptación del proveedor separada de recepción humana o reproducción audiovisual.
4. Compruebe reinicio, reservas inciertas y limpieza con datos sintéticos antes de usar un proveedor real. Un restart borra memoria conversacional; **no** borra PostgreSQL.
5. Si hay incertidumbre, detenga operaciones y concilie. No reintente envíos ni creaciones ciegamente. Copias de seguridad y rotación de claves requieren preservar la capacidad de descifrar estado existente.

## Fuentes oficiales

- [Procfile](https://devcenter.heroku.com/articles/procfile), [Python y .python-version](https://devcenter.heroku.com/articles/python-runtimes).
- [Disco efímero y SQLite](https://devcenter.heroku.com/articles/sqlite3), [timeout HTTP](https://devcenter.heroku.com/articles/request-timeout), [apagado](https://devcenter.heroku.com/articles/dyno-shutdown-behavior).
- [Chainlit archivado](https://docs.chainlit.io/get-started/project-status), [Open WebUI](https://docs.openwebui.com/getting-started/quick-start/).

La entrega de estas fuentes no demuestra build Linux, tamaño de slug, rendimiento cloud ni integración con cuentas. Esas comprobaciones requieren un despliegue y autorización separados.
