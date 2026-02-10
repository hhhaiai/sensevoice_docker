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

## 5. 防休眠 Keep-Alive（可选）

仓库已提供定时任务：`.github/workflows/hf-keepalive.yml`

- 默认每 20 分钟调用一次 `GET /health`
- 支持手动触发 `workflow_dispatch`
- 默认地址：`https://sanbo1200-sensevoice.hf.space`

如果你的 Space 地址变更，建议在 GitHub 仓库里设置变量：

- `Settings -> Secrets and variables -> Actions -> Variables`
- 新增：`HF_SPACE_URL=https://<your-space-name>.hf.space`

说明：

- 免费资源策略下，平台仍可能在长时间后休眠，定时心跳只能降低休眠概率，不保证 100% 常驻。
