# Glados自动签到

## 食用方式：

### 注册一个GLaDOS的账号([注册地址](https://glados.space/landing/0A58E-NV28S-6U3QV-33VMG))

#### 我的邀请码：([0A58E-NV28S-6U3QV-33VMG](https://0a58e-nv28s-6u3qv-33vmg.glados.space)) 

#### 我的优惠码（9折）：([DEVILSTORE](https://0a58e-nv28s-6u3qv-33vmg.glados.space)) 

### **Fork**本仓库

![图片加载失败](imgs/1.png)

### 添加**secret**

1. 跳转至自己的仓库的`Settings`->`Secrets and variables`->`Action`

2. 添加1个`repository secret`，命名为`GLADOS_COOKIES`，其值对应GLaDOS账号的cookie值中的有效部分（获取方式如下）

- 在GLaDOS的签到页面按`F12`

- 切换到`Network`页面下，刷新

![图片加载失败](imgs/2.png)

- 点击第一个选项卡后在`Request Headers`下找到`Cookie`，右键复制cookie的值即可

  > 参考格式（最小可用）：`gld:sess=gld_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx; gld:sess.sig=xxxxxxxxxxxxxxxxxxxxxxxxxxx`
  >
  > ✅ **只需 `gld:sess` + `gld:sess.sig` 两个 cookie**，实测在 `glados.cloud` / `glados.space` / `glados.network` 均可通过校验。
  >
  > `koa:sess` / `koa:sess.sig` 是旧版会话，已失效、可省略；只带这两个会返回 `{"code":-2,"message":"没有权限"}`。

- Safari：先在「设置 → 高级」勾选「显示网页开发者功能」，在 GLaDOS 页面按 `Option+Command+I` 打开 Web Inspector，切到 `Network` 刷新，找到 `/api/user/status` 请求，在请求头里复制 `Cookie` 的值。

![图片加载失败](imgs/3.png)

- 多账号请在 `COOKIES` 中 添加多个 `cookies` 中间使用 `&`连接即可。（例如： `c1&c2&c3`）

3. 配置积分兑换策略（非必须）

- 添加1个`repository secret`，命名为`GLADOS_EXCHANGE_PLAN`，配置自动兑换积分策略：

| 值 | 积分要求 | 兑换天数 |
|---|---------|---------|
| `plan100` | 100 积分 | 10 天 |
| `plan200` | 200 积分 | 30 天 |
| `plan500` | 500 积分 | 100 天 (默认) |
| `none` | - | 关闭自动兑换 |

> 不配置时默认为 `plan500`，即积分达到 500 时自动兑换 100 天

- 按账号单独配置兑换策略（非必须）：在 `.github/workflows/gladosCheck.yml` 的签到步骤 `env` 中配置 `GLADOS_EXCHANGE_PLANS`，多个账号使用 `&` 分隔，与 `GLADOS_COOKIES` 中的 Cookie 顺序一一对应：

```yaml
GLADOS_EXCHANGE_PLANS: 'plan500&none'
```

> 上例表示：第 1 个账号使用 `plan500`，第 2 个账号关闭自动兑换。未配置的账号（如 Cookie 数量多于计划数量时）回落到 `GLADOS_EXCHANGE_PLAN`（默认 `plan500`）

4. 手机推送（非必须）

支持多个推送后端，按需配置对应 `repository secret` 即可，**配置几个就发几个（都发）**：

- **PushDeer**：添加 `repository secret` 命名为 `PUSHDEER_SENDKEY`，其值对应 PushDeer key（[获取地址](https://www.pushdeer.com/product.html)）。
- **Server酱**：添加 `repository secret` 命名为 `SERVERCHAN_SENDKEY`，其值对应 Server酱 SendKey（[获取地址](https://sct.ftqq.com/)）。

### **star**自己的仓库

![图片加载失败](imgs/4.png)

## 文件结构

```shell
│  checkin.py          # 签到主脚本
│  logging_config.py   # 日志配置（北京时间、[I/W/E] 短标签）
│  requirements.txt    # Python 依赖
│
├─.github
│  └─workflows
│          gladosCheck.yml   # Actions 配置文件
```

## 更新日志

- **2026-01**: 重构代码，添加log输出方便定位，支持新版网址，支持配置积分兑换策略。
- **2026-04**: 优化代码逻辑，优化日志输出，支持[新版域名](https://railgun.info) ，在 GLADOS_COOKIES 中添加新版域名下的 cookies 即可使用。
- **2026-09**: 适配 GLaDOS 新增的 `gld:sess` cookie（缺失会导致 `没有权限`）；全部账号签到失败时脚本以非零状态退出，使 Actions 正确显示失败。
- **2026-09**: 新增设备平台自适应重试：签到返回 `code 4` 时自动读取服务端 `loginDevice` 并切换对应平台 UA 重试，无需手动配置 User-Agent。
- **2026-09**: 新增网络层重试/退避：`requests.Session` 挂载 `HTTPAdapter` + `urllib3.Retry`（total=3、指数退避 1/2/4s、对 429/5xx 自动重试），降低 CI 网络抖动导致的误报失败；业务 200 响应不受影响。
- **2026-10**: 推送支持 Server酱（多后端「都发」策略）；日志级别短标签 `[I]/[W]/[E]` 对齐；新增 `requirements.txt`。
- **2026-10**: Cookie 精简为 `gld:sess` + `gld:sess.sig`（`koa:sess` 已废弃，无需再配置）。


## 问题排查与定位
- 大家可以通过查询 actions 中的 running checkin 日志快速定位问题，有其他问题提交issue。
- 若日志出现 `code : 4, message : Automated check-in detected`：这是 GLaDOS 的**设备平台校验**（`reason=device-mismatch`，即签到请求 UA 平台与登录设备平台不一致）。脚本已内置**设备平台自适应**：首次失败时自动读取服务端返回的 `loginDevice` 并切换对应平台 UA 重试，**无需手动配置 User-Agent**；若长期仍失败，请重新登录 glados.cloud 手动签到一次并刷新 cookie。

  <img width="1684" height="844" alt="image" src="https://github.com/user-attachments/assets/45348a5f-43e4-45f5-8fdf-ce84d343b30d" />

## 声明

本项目不保证稳定运行与更新, 因GitHub相关规定可能会删库, 请注意备份







