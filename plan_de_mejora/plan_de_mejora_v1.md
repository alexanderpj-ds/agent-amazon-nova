# Plan de Mejora v1 — DatIA Voice (agent-amazon-nova)

## Contexto del caso de uso

DatIA Voice será usado en presentaciones en salón de reuniones con múltiples asistentes. Las preguntas al agente son intermitentes — puede haber pausas de varios minutos entre una pregunta y la siguiente. Este contexto define los requisitos de mejora.

---

## Problema actual

Nova Sonic tiene dos límites que generan problemas en este caso de uso:

| Límite | Valor | Consecuencia actual |
|---|---|---|
| Timeout de inactividad | 55 segundos | Si el micrófono está apagado más de 55s, Nova Sonic cierra el stream. El frontend muestra "Desconectado" y la conversación se pierde |
| Duración máxima de sesión | 8 minutos | Independientemente de la actividad, la sesión se cierra. En una presentación de 1 hora esto ocurre múltiples veces |

---

## Solución recomendada: Reconexión automática con historial

Basada en la documentación oficial de AWS ([Managing chat history](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-chat-history.html)) y la guía de manejo de errores ([Handling errors with Amazon Nova Sonic](https://docs.aws.amazon.com/nova/latest/userguide/speech-errors.html)).

AWS recomienda explícitamente para este patrón:
> *"When approaching the maximum connection duration, end the current request and start a new one. Include the saved chat history in the new request to maintain conversation continuity."*

### Principio de funcionamiento

1. Durante la conversación, el servidor guarda en memoria el historial completo (transcripciones de usuario y respuestas de DatIA)
2. Cuando la sesión expira o hay timeout, el servidor crea una nueva sesión automáticamente
3. Al iniciar la nueva sesión, reinyecta el historial guardado antes de abrir el stream de audio
4. El usuario no percibe la interrupción — DatIA recuerda todo lo que se habló

### Por qué esta alternativa y no keepalive con silencio

La alternativa de enviar chunks de silencio (bytes en cero) para mantener el stream activo fue descartada porque:
- Consume tokens de audio aunque nadie esté hablando
- En una presentación de 1 hora con pausas largas, el costo puede ser significativo
- No es el patrón recomendado por AWS para este tipo de caso de uso

---

## Especificaciones técnicas de la implementación

### 1. Almacenamiento del historial

La documentación especifica:
- El historial se construye a partir de los eventos `textOutput` con `generationStage: FINAL` (no SPECULATIVE)
- Límite total del historial: **40KB**
- Límite por mensaje individual: **1KB** (si supera 1KB, dividir en múltiples `textInput` dentro del mismo bloque)
- Si el historial supera 40KB, recortar los mensajes más antiguos

Estructura a guardar en memoria por cada turno:
```python
{
    "role": "USER" | "ASSISTANT",
    "content": "texto de la transcripción"
}
```

### 2. Detección de expiración de sesión

Hay dos eventos que indican que la sesión expiró y hay que reconectar:

- **Timeout de inactividad (55s)**: Nova Sonic cierra el stream con error. El servidor lo detecta en `_process_responses` cuando la excepción no es `CancelledError`.
- **Límite de 8 minutos**: Nova Sonic envía un `ModelTimeoutException`. El servidor debe detectarlo y reconectar proactivamente antes de que ocurra (a los ~7 minutos).

### 3. Flujo de reconexión

```
Sesión activa
    │
    ├── Timeout (55s inactividad) o límite 8 min
    │
    ▼
Guardar historial en memoria
    │
    ▼
Cerrar sesión actual (promptEnd → sessionEnd)
    │
    ▼
Crear nueva sesión Nova Sonic
    │
    ▼
Enviar sessionStart + promptStart
    │
    ▼
Enviar system prompt
    │
    ▼
Enviar historial guardado (contentStart/textInput/contentEnd por cada mensaje)
    │
    ▼
Abrir stream de audio
    │
    ▼
Notificar al frontend: sesión restaurada
```

### 4. Formato de reinyección del historial

Según la documentación, cada mensaje del historial requiere tres eventos en este orden:

```python
# contentStart
{
    "event": {
        "contentStart": {
            "promptName": "<prompt-id>",
            "contentName": "<content-id-unico>",
            "type": "TEXT",
            "interactive": True,
            "role": "USER" | "ASSISTANT",
            "textInputConfiguration": {"mediaType": "text/plain"}
        }
    }
}

# textInput
{
    "event": {
        "textInput": {
            "promptName": "<prompt-id>",
            "contentName": "<content-id-unico>",
            "content": "texto del mensaje"
        }
    }
}

# contentEnd
{
    "event": {
        "contentEnd": {
            "promptName": "<prompt-id>",
            "contentName": "<content-id-unico>"
        }
    }
}
```

Restricciones importantes:
- El historial se envía **una sola vez por sesión**
- Debe enviarse **después del system prompt y antes de abrir el stream de audio**
- El primer mensaje del historial debe ser de rol `USER`
- Los mensajes deben alternarse: USER → ASSISTANT → USER → ASSISTANT...

### 5. Reconexión proactiva antes del límite de 8 minutos

Para evitar que la sesión se corte en medio de una respuesta, el servidor debe monitorear el tiempo de sesión y reconectar proactivamente:

```python
SESSION_MAX_SECONDS = 7 * 60  # reconectar a los 7 min, antes del límite de 8

async def session_watchdog(self):
    """Reconecta proactivamente antes del límite de 8 minutos."""
    await asyncio.sleep(SESSION_MAX_SECONDS)
    if self.is_active:
        logger.info("Approaching session limit — reconnecting proactively")
        await self.reconnect_with_history()
```

---

## Archivos a modificar

| Archivo | Cambio |
|---|---|
| `nova_sonic_client.py` | Agregar: almacenamiento de historial, método `reconnect_with_history()`, método `_inject_history()`, watchdog de sesión |
| `server.py` | Agregar: notificación al frontend cuando la sesión se restaura (`{"type": "session_restored"}`) |
| `frontend/index.html` | Agregar: manejo del evento `session_restored` para mostrar indicador visual de reconexión |

---

## Consideraciones adicionales

### Límite de 40KB del historial

Para una presentación de 1 hora con preguntas frecuentes, el historial puede crecer. La estrategia de recorte recomendada es **sliding window**: mantener siempre los últimos N intercambios (por ejemplo, los últimos 10 turnos), descartando los más antiguos cuando se supera el límite de 40KB.

### Privacidad del historial

El historial se almacena en memoria del servidor (no en base de datos). Al terminar la sesión con "Terminar sesión", el historial se borra completamente. Esto es adecuado para el caso de uso de presentación.

### Indicador visual en el frontend

Cuando ocurre una reconexión, el frontend debe mostrar brevemente un indicador como "Reconectando..." para que el presentador sepa que el sistema está restaurando el contexto. La conversación en el panel derecho no debe borrarse.

---

## Referencias oficiales

- [Managing chat history — Nova 2 Sonic](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-chat-history.html)
- [Handling errors with Amazon Nova Sonic](https://docs.aws.amazon.com/nova/latest/userguide/speech-errors.html)
- [Session Continuation example — AWS Nova Samples GitHub](https://github.com/aws-samples/amazon-nova-samples)
- [Getting started with speech-to-speech](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-getting-started.html)
