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

安装依赖：

```bash
pip install -r requirements.txt
```

启动服务：

```bash
python server.py
```

访问网页：

```text
http://127.0.0.1:8008/
```

## 4. API 说明

### 4.1 健康检查

`GET /health`

### 4.2 PCM 流转写（兼容 client.py）

`POST /transcribe_stream`  
`POST /api/transcribe/pcm`

- Header: `Content-Type: application/octet-stream`
- Body: 16kHz 单声道 `int16` PCM 字节流

### 4.3 音频文件转写

`POST /api/transcribe/file`

- `multipart/form-data`
- 字段：`file`（音频文件）

支持常见格式，服务端会调用 `ffmpeg` 自动转为 16kHz 单声道 WAV 再推理。

### 4.4 WebSocket 实时转写

`WS /ws/transcribe`

- 发送二进制帧：16kHz 单声道 `int16` PCM
- 支持文本指令：
  - `{"event":"flush"}` 输出最终结果并清空缓冲
  - `{"event":"end"}` 输出最终结果并结束
  - `{"event":"reset"}` 清空当前会话

## 5. Docker 部署

构建镜像：

```bash
./auto_merge.sh
docker build -t sensevoice-service:local .
```

运行容器：

```bash
docker run --rm -p 8008:8008 sensevoice-service:local
```

或使用 compose：

```bash
docker compose up --build
```

## 6. GitHub Actions 发布到 GHCR

工作流文件：`.github/workflows/docker-ghcr.yml`

触发条件：
- 推送到 `main`
- 推送 `v*` 标签
- 手动触发 `workflow_dispatch`

发布流程中会先执行：

```bash
./auto_merge.sh
```

然后构建并推送镜像到：

```text
ghcr.io/<owner>/<repo>
```

使用 GHCR 镜像：

```bash
docker pull ghcr.io/<owner>/<repo>:latest
docker run --rm -p 8008:8008 ghcr.io/<owner>/<repo>:latest
```
