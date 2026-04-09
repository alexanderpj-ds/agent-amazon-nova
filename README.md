# DatIA Voice — Amazon Nova 2 Sonic

Agente de voz para gobierno de datos usando Amazon Nova 2 Sonic (speech-to-speech nativo). El usuario habla, el modelo entiende y responde con voz — sin pasos intermedios de transcripción ni síntesis.

## Arquitectura

```
Navegador (micrófono) → WebSocket → FastAPI server → Nova 2 Sonic (Bedrock)
                                                              ↓
Navegador (altavoz)   ← WebSocket ← FastAPI server ← audio chunks
```

## Requisitos

- Python 3.8+
- Cuenta AWS con acceso a `amazon.nova-2-sonic-v1:0` en us-east-1
- Micrófono y altavoces

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración de credenciales

El SDK `aws-sdk-bedrock-runtime` usa variables de entorno (no perfiles SSO directamente). Exporta las credenciales temporales desde tu perfil:

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

Presiona el orbe para hablar. El agente responde con voz en español.

## Estructura

```
agent-amazon-nova/
├── server.py              # FastAPI + WebSocket server
├── nova_sonic_client.py   # Lógica de sesión bidireccional con Nova Sonic
├── Dockerfile             # Imagen del servidor (compatible con Podman)
├── requirements.txt
├── template.yaml          # CloudFormation: ECS Fargate + ALB + S3
├── .env.example
├── commands/
│   ├── deploy_backend.sh  # Build imagen + push ECR + deploy CloudFormation
│   ├── deploy_frontend.sh # Subir frontend a S3
│   └── update_image.sh    # Rebuild + redeploy sin cambiar infraestructura
├── frontend/
│   └── index.html
└── plan_de_mejora/
    └── plan_de_mejora_v1.md
```

## Despliegue en AWS

### Prerrequisitos
- Podman instalado
- AWS CLI configurado con perfil `alx-dev`
- Acceso a `amazon.nova-2-sonic-v1:0` en Bedrock (us-east-1)

### Paso 1 — Deploy del backend (ECS Fargate + ALB + S3)
```bash
chmod +x commands/deploy_backend.sh
./commands/deploy_backend.sh
```
Este script:
1. Crea el repositorio ECR `datia-voice` si no existe
2. Autentica Podman con ECR
3. Construye la imagen con Podman
4. Hace push a ECR
5. Despliega el stack CloudFormation (VPC, ALB, ECS Fargate, S3)
6. Muestra los outputs con las URLs

### Paso 2 — Actualizar WS_URL en el frontend
Copia el `WebSocketURL` del output del deploy y actualiza en `frontend/index.html`:
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

### Actualizar solo el código (sin cambiar infraestructura)
```bash
./commands/update_image.sh
```

## Notas técnicas

- Audio entrada: PCM 16-bit, 16kHz, mono (capturado con AudioWorklet)
- Audio salida: PCM 16-bit, 24kHz, mono (reproducido con AudioContext)
- Sesión máxima: 8 minutos (límite de Nova Sonic)
- Model ID: `amazon.nova-2-sonic-v1:0`
- Voz: `tiffany` (femenina, inglés/español)
