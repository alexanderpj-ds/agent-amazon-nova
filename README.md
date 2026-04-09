# DatIA Voice — Amazon Nova 2 Sonic

Agente de voz para gobierno de datos e inteligencia artificial usando Amazon Nova 2 Sonic (speech-to-speech nativo). El usuario habla o escribe, el modelo entiende y responde con voz — sin pasos intermedios de transcripción ni síntesis.

## Arquitectura

```
Navegador (micrófono/texto) → WebSocket → FastAPI server → Nova 2 Sonic (Bedrock)
                                                                   ↓
Navegador (altavoz)         ← WebSocket ← FastAPI server ← audio chunks
```

## Características

- **Entrada de voz**: Habla directamente con el agente
- **Entrada de texto**: Escribe mensajes que el agente responde con voz (cross-modal)
- **Reconexión automática**: Si la sesión expira, el sistema reconecta automáticamente
- **Prompt configurable**: El system prompt se carga desde `prompts/PromptDatIA_v2.md`

## Requisitos

- Python 3.12+
- Cuenta AWS con acceso a `amazon.nova-2-sonic-v1:0` en us-east-1
- Micrófono y altavoces
- Podman o Docker (para despliegue)

## Instalación local

```bash
pip install -r requirements.txt
```

## Configuración de credenciales

El SDK `aws-sdk-bedrock-runtime` usa variables de entorno. Exporta las credenciales desde tu perfil:

```bash
# En bash/zsh:
eval $(aws configure export-credentials --profile alx-dev --format env)

# En Windows PowerShell:
aws configure export-credentials --profile alx-dev --format env-no-export
# Luego copia y ejecuta las líneas $env:AWS_... que aparecen
```

## Uso local

```bash
uvicorn server:app --host 0.0.0.0 --port 8080
```

Abre `frontend/index.html` en el navegador (Chrome recomendado).

Presiona el orbe para hablar o usa el campo de texto para escribir.

## Estructura del proyecto

```
agent-amazon-nova/
├── server.py                # FastAPI + WebSocket server
├── nova_sonic_client.py     # Cliente bidireccional con Nova Sonic
├── Dockerfile               # Imagen del servidor
├── requirements.txt
├── template.yaml            # CloudFormation: ECS Fargate + ALB + S3
├── .env.example
├── commands/
│   ├── deploy_backend.sh    # Deploy inicial de infraestructura
│   ├── deploy_frontend.sh   # Subir frontend a S3
│   └── update_image.sh      # Actualizar código sin cambiar infra
├── frontend/
│   └── index.html           # SPA con AudioWorklet
├── prompts/
│   └── PromptDatIA_v2.md    # System prompt del agente
└── plan_de_mejora/
    └── plan_de_mejora_v1.md # Documentación de mejoras futuras
```

## Despliegue inicial en AWS

### Prerrequisitos
- Podman instalado y corriendo (`podman machine start`)
- AWS CLI configurado con perfil `alx-dev`
- Acceso a `amazon.nova-2-sonic-v1:0` en Bedrock (us-east-1)

### Paso 1 — Deploy del backend
```bash
chmod +x commands/deploy_backend.sh
./commands/deploy_backend.sh
```

Este script:
1. Crea el repositorio ECR `datia-voice` si no existe
2. Construye la imagen con Podman (linux/amd64)
3. Hace push a ECR
4. Despliega el stack CloudFormation (VPC, ALB, ECS Fargate, S3)

### Paso 2 — Configurar WS_URL en el frontend
Copia el `WebSocketURL` del output y actualiza en `frontend/index.html`:
```javascript
const WS_URL = "ws://TU_ALB_DNS/ws";
```

### Paso 3 — Deploy del frontend
```bash
chmod +x commands/deploy_frontend.sh
./commands/deploy_frontend.sh
```

### Paso 4 — Acceder
Abre la `FrontendURL` del output en el navegador.

---

## Despliegue después de actualizaciones

Cuando modifiques el código (server.py, nova_sonic_client.py, prompts, etc.) sin cambiar la infraestructura:

### Prerrequisitos
```bash
# Asegúrate de que Podman esté corriendo
podman machine start
```

### Comando de actualización
```bash
# Desde la raíz del proyecto
./commands/update_image.sh
```

Este script:
1. Construye una nueva imagen Docker (linux/amd64 para ECS Fargate)
2. Autentica con ECR
3. Hace push de la nueva imagen
4. Fuerza un nuevo deployment en ECS (el servicio descarga la nueva imagen)

El proceso toma ~2-3 minutos. ECS reemplaza automáticamente las tareas con la nueva versión.

### Verificar el despliegue
Revisa los logs en CloudWatch para confirmar que el nuevo código está corriendo:
```bash
aws logs tail /ecs/datia-voice --follow --profile alx-dev
```

### Archivos que requieren re-deploy
- `server.py` — Lógica del servidor WebSocket
- `nova_sonic_client.py` — Cliente de Nova Sonic
- `prompts/PromptDatIA_v2.md` — System prompt del agente
- `requirements.txt` — Dependencias Python

### Archivos que NO requieren re-deploy del backend
- `frontend/index.html` — Usar `./commands/deploy_frontend.sh`
- `template.yaml` — Usar `./commands/deploy_backend.sh`

---

## Modificar el prompt del agente

El system prompt está en `prompts/PromptDatIA_v2.md`. Para actualizarlo:

1. Edita el archivo `prompts/PromptDatIA_v2.md`
2. Ejecuta `./commands/update_image.sh`
3. Espera ~2 minutos a que ECS despliegue la nueva versión

El prompt se carga automáticamente al iniciar cada sesión.

---

## Notas técnicas

| Parámetro | Valor |
|-----------|-------|
| Audio entrada | PCM 16-bit, 16kHz, mono |
| Audio salida | PCM 16-bit, 24kHz, mono |
| Sesión máxima | 8 minutos (límite Nova Sonic) |
| Timeout inactividad | ~295 segundos (reconexión automática) |
| Model ID | `amazon.nova-2-sonic-v1:0` |
| Voz | `lupe` (femenina, español nativo) |
| Región | us-east-1 |

## Comportamiento de reconexión

El agente maneja automáticamente los timeouts de Nova Sonic:
- Si no hay actividad por ~5 minutos, Nova Sonic cierra la sesión
- El servidor detecta el cierre y reconecta automáticamente en ~1 segundo
- El usuario puede seguir usando el agente sin interrupción
- Nota: El historial de conversación se pierde en cada reconexión

## Troubleshooting

### Error: "exec format error"
La imagen se construyó para la arquitectura incorrecta. Asegúrate de que `update_image.sh` use `--platform linux/amd64`.

### Error: "permission denied" al ejecutar scripts
```bash
chmod +x commands/*.sh
```

### Error: "Cannot connect to Podman"
```bash
podman machine start
```

### El agente no responde después de inactividad
Esto es normal. El sistema reconecta automáticamente. Espera ~1 segundo y vuelve a intentar.
