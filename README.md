# SenseVoice Docker Service

本项目提供本地 SenseVoice 模型的语音转文字服务，支持：
- HTTP API（PCM 流、音频文件上传）
- WebSocket 实时语音转写
- 内置网页调用（文件转写 + 浏览器麦克风实时转写）
- Docker 一键部署
- GitHub Actions 自动构建并推送到 GitHub Container Registry（GHCR）

## 1. 目录说明

- `server.py`: 服务端（FastAPI）
- `client.py`: 命令行录音示例客户端（兼容旧接口）
- `web/index.html`: 内置网页
- `auto_merge.sh`: 模型分片合并脚本
- `Dockerfile`: 容器构建文件
- `.github/workflows/docker-ghcr.yml`: GHCR 自动构建发布

## 2. 启动前准备

你的模型被拆分为 `.part*` 文件，启动前先合并：

```bash
chmod +x auto_merge.sh
./auto_merge.sh
```

## 3. 本地运行

安装服务依赖：

```bash
pip install -r requirements-server.txt
```

启动服务：

```bash
python server.py
```

如果要使用命令行录音示例 `client.py`，再额外安装：

```bash
pip install -r requirements.txt
```

访问网页：

```text
http://127.0.0.1:7860/
```

## 4. API 说明

### 4.1 健康检查

`GET /health`

### 4.2 PCM 流转写（兼容 client.py）

`POST /transcribe_stream`  
`POST /api/transcribe/pcm`  
`POST /transcribe/pcm`

- Header: `Content-Type: application/octet-stream`
- Body: 16kHz 单声道 `int16` PCM 字节流

### 4.3 音频文件转写

`POST /api/transcribe/file`  
`POST /transcribe/file`

- `multipart/form-data`
- 字段：`file`（音频文件）

支持常见格式，服务端会调用 `ffmpeg` 自动转为 16kHz 单声道 WAV 再推理。

### 4.4 WebSocket 实时转写

`WS /ws/transcribe`  
`WS /ws`

- 发送二进制帧：16kHz 单声道 `int16` PCM
- 支持文本指令：
  - `{"event":"flush"}` 输出最终结果并清空缓冲
  - `{"event":"end"}` 输出最终结果并结束
  - `{"event":"reset"}` 清空当前会话

### 4.5 curl 调用示例

健康检查：

```bash
curl -sS http://127.0.0.1:7860/health
```

PCM 转写（示例发送 1 秒静音）：

```bash
ffmpeg -f lavfi -i anullsrc=r=16000:cl=mono -t 1 -f s16le -ac 1 -ar 16000 -y /tmp/demo.pcm
curl -sS -X POST http://127.0.0.1:7860/transcribe/pcm \
  -H "Content-Type: application/octet-stream" \
  --data-binary @/tmp/demo.pcm
```

音频文件转写：

```bash
curl -sS -X POST http://127.0.0.1:7860/transcribe/file \
  -F "file=@/path/to/demo.wav;type=audio/wav"
```

## 5. Docker 部署

### 5.1 本地构建镜像

```bash
./auto_merge.sh
docker build -t sensevoice-service:local .
```

### 5.2 本地运行容器

```bash
docker run --rm -p 7860:7860 sensevoice-service:local
```

带环境变量运行（推荐）：

```bash
docker run --rm -p 7860:7860 \
  -e PORT=7860 \
  -e MODEL_PATH=sensevoice-small \
  -e MAX_CONCURRENT_INFERENCE=2 \
  -e INFERENCE_TIMEOUT_SEC=45 \
  sensevoice-service:local
```

### 5.3 使用 Docker Compose

```bash
docker compose up --build
```

### 5.4 容器运行后快速验证

```bash
curl -sS http://127.0.0.1:7860/health
curl -sS -X POST http://127.0.0.1:7860/transcribe/pcm \
  -H "Content-Type: application/octet-stream" \
  --data-binary @/dev/null
```

## 6. GitHub Actions 发布到 GHCR

工作流文件：`.github/workflows/docker-ghcr.yml`

触发条件：
- 推送任意 Git 标签（tag）
- 手动触发 `workflow_dispatch`

发布流程中会先执行：

```bash
./auto_merge.sh
```

然后构建并推送镜像到：

```text
ghcr.io/<owner>/<repo>
```

### 6.1 使用 GHCR 镜像

```bash
docker pull ghcr.io/<owner>/<repo>:<your-tag>
docker run --rm -p 7860:7860 ghcr.io/<owner>/<repo>:<your-tag>
```

建议使用显式标签，例如：

```bash
docker pull ghcr.io/<owner>/<repo>:v1.0.0
docker run --rm -p 7860:7860 ghcr.io/<owner>/<repo>:v1.0.0
```

如果镜像是私有仓库，先登录：

```bash
echo <GH_PAT> | docker login ghcr.io -u <github-username> --password-stdin
```

## 7. Hugging Face Spaces 兼容说明

- 默认端口已调整为 `7860`（可通过环境变量 `PORT` 覆盖）
- 不依赖 OpenAI 风格路径，不需要 `/v1/*`
- 推荐在 Spaces 使用这些路径：
  - `POST /transcribe/file`
  - `POST /transcribe/pcm`
  - `WS /ws`
