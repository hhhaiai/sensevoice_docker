# Hugging Face Spaces 部署说明

## 1. Space 类型

创建 **Docker Space**，仓库代码直接上传。

## 2. 关键点

- 服务默认端口是 `7860`
- 服务接口不依赖 `/v1/*`
- 启动时会自动检测模型分片并执行 `auto_merge.sh`（默认开启）

可选环境变量：

- `PORT=7860`
- `MODEL_PATH=sensevoice-small`
- `AUTO_MERGE_ON_STARTUP=true`
- `MAX_CONCURRENT_INFERENCE=2`
- `INFERENCE_TIMEOUT_SEC=45`

## 3. 验证接口

部署完成后先访问：

- `GET /health`
- `GET /`

再测试：

- `POST /transcribe/file`
- `WS /ws`

## 4. 与 GitHub Pages 联动

你的 GitHub Pages 页面只负责前端展示，后端地址填写为：

`https://<your-space-name>.hf.space`

页面会调用：

- `https://<your-space-name>.hf.space/transcribe/file`
- `wss://<your-space-name>.hf.space/ws`
