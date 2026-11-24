# 长江雨课堂自动化助手（YuketangAuto）

面向长江雨课堂（changjiang.yuketang.cn）的课程自动化工具，支持视频进度自动推进与作业自动提交，提供图形界面和命令行两种使用方式。

> ⚠️ **免责声明**
> 本工具仅供学习交流与个人效率研究使用。使用本工具产生的任何后果（包括但不限于账号风控、课程成绩异常、学术诚信问题）由使用者自行承担。请遵守所在学校的规定，合理使用。

---

## 功能

- 🌐 **浏览器登录抓 Cookie**：自动弹出浏览器，扫码 / 密码登录后自动抓取并保存登录态
- 📺 **视频进度推进**：支持 `sim`（真实 1×）/ `5×` / `10×` / `20×` 多档速度，断点续刷、限流自愈、降速自愈
- 📝 **作业自动提交**：利用接口下发的参考答案自动完成作业（已提交的自动跳过）
- 🖥️ **图形界面**：Tkinter 界面，实时进度条 + 彩色日志 + 一键操作
- 📋 **命令行**：支持批量课程、单课程、仅视频 / 仅作业等模式

## 使用方式

### 方式一：直接运行 exe（现已不支持exe，不用看着一条了）

1. 下载 `dist/YuketangAuto.exe`
2. 双击运行
3. 点击「🌐 浏览器登录抓 Cookie」完成登录
4. 选择课程序号与执行模式，开始运行

> 首次运行可能被 Windows Defender 误报（PyInstaller 打包程序的常见现象），选择「仍要运行」即可。

### 方式二：源码运行（需要 Python 3.12+）

```bash
pip install -r requirements.txt
python gui.py            # 图形界面
python yuketang.py ls    # 命令行：列出课程
python yuketang.py 1     # 命令行：刷第 1 门课
```

Windows 用户可直接双击 `图形窗口.bat`（图形版）或 `启动.bat`（控制台菜单）。

## 配置

复制 `config.example.json` 为 `config.json` 后按需修改：

| 字段 | 说明 | 可选值 |
|---|---|---|
| `video_speed` | 视频推进倍速 | `sim`（真实 1×）/ `5` / `10` / `20` |
| `mode` | 默认执行模式 | `all` / `video` / `homework` |
| `homework.uv_id` | 学校 uv_id（mooc-api 报错时填写） | 数字字符串 |
| `homework.mooc_base` | mooc-api 基地址（一般留空） | URL |

## 目录结构

```
YuketangAuto/
├── dist/
│   └── YuketangAuto.exe    # 打包好的图形版（无需 Python）
├── gui.py                  # 图形界面
├── yuketang.py             # 核心协议（视频心跳 / 作业提交）
├── browser_login.py        # 浏览器登录抓 Cookie
├── tee_log.py              # 命令行日志管道
├── 启动.bat                # 控制台菜单启动器（GBK 编码）
├── 图形窗口.bat            # 图形界面启动器（GBK 编码）
├── requirements.txt        # Python 依赖
└── config.example.json     # 配置模板
```

> 注意：`.bat` 文件必须以 **GBK / cp936 编码 + CRLF 换行**保存，否则 cmd.exe 会解析中文出错。

## 隐私说明

- 本工具**不收集、不上传**任何个人信息
- 登录凭据（Cookie）仅保存在本地 `cookie.txt`，已被 `.gitignore` 排除
- 运行日志仅保存在本地 `运行日志/`，同样已排除
- 打包的 exe 不含任何开发者或使用者的设备路径、账号信息

## 常见问题

| 问题 | 处理 |
|---|---|
| 提示 Cookie 失效 | 重新点击「浏览器登录抓 Cookie」登录一次 |
| 浏览器一闪就关 | 检查 Edge / Chrome 是否安装，或重试一次 |
| 视频进度不涨 | 接口可能已改版，尝试降低倍速 |
| 作业提交限流 | 正常现象，程序会自动等待后重试 |
| `No module named 'requests'` | 用 `pip install -r requirements.txt` 安装依赖 |
