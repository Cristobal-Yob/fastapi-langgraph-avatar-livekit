# Procedencia

La base procede del starter local FastAPI/LangGraph revisado el 27 de octubre de 2026. Se copian únicamente fuentes allowlisted; no historial Git, configuración privada, entornos ni estado de cuentas.

`app/graph.py`, `app/prompts.py`, `app/provider.py` y `app/memory.py` conservan los bytes de la base. `app/email_delivery.py` conserva la gramática y planificación, con una frontera cloud nueva. `cloud/` añade autenticación de un operador, trabajos asincrónicos y reservas PostgreSQL antes de efectos. Los adaptadores propios conservan guardas de identidad, voz y/o SDK según corresponda; configuración local Windows/archivos se reemplaza por variables y estado cifrado externo, nunca por eliminación de controles.

Hashes SHA-256 de la base:

```json
{
  "graph.py": "aaa18441525fd8d9c4ed8ad43c2551e5349f9756ad449bbc52ec80341e979b8a",
  "prompts.py": "67806e3ff5656a3933c22c9d556fdafa0e1dd511f6a60d26e700ba23a3cff783",
  "provider.py": "0b4a2851c9ffaf6968868920f04258e45f90ced8dceda4d6b5d082dfa0fffb9e",
  "memory.py": "002ce0bbe03f18d1e38c14dea7205f6b78aa3397de885d33da8c8bddc14819bf"
}
```

El origen queda intacto y no es una dependencia runtime. No se han copiado credenciales, contenido del usuario, audios, registros ni carpetas privadas. Esta preparación no prueba entrega real.
