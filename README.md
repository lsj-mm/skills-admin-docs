# Skills Admin 工具说明自动更新

Watch a folder of tools and keep `工具说明.md` in sync: name, short description, and GitHub or official website. Runs in the background on Windows, Linux, and macOS.

把工具放进同一个文件夹，程序会自动维护一份 `工具说明.md` 表格（名称、简要说明、GitHub / 官网链接）。支持 Windows、Linux、macOS 后台运行和开机自启。

## 做什么

把本仓库整个文件夹放进你存放软件、压缩包、项目的目录里。之后往那个目录放入或删除文件时，表格会自动增删对应行。

已经写好的说明和链接不会被覆盖，可以随时改成中文或更准确的描述。

## 要求

- Python 3.8 或更高版本
- 联网可选：用来补充 GitHub 简介、搜索官网，以及（可选）百度翻译

## 安装

### Windows

1. 把本文件夹放到工具目录中（例如 `D:\Tools\工具说明程序`）。
2. 复制 `config.example.json` 为 `config.json`。
3. 双击 `安装开机自启.bat`。

取消：双击 `取消开机自启.bat`。

### Linux

1. 把本文件夹放到工具目录中。
2. 复制示例配置：`cp config.example.json config.json`
3. 在本文件夹中执行：`sh 安装开机自启.sh`

有 systemd 时会安装用户服务（登录后启动）。没有 systemd 时会写入桌面自启动项。若希望开机未登录也运行：

```bash
loginctl enable-linger "$USER"
```

取消：`sh 取消开机自启.sh`

### macOS

1. 安装 Python 3（终端输入 `python3` 时若提示安装，按提示即可）。
2. 把本文件夹放到工具目录中。
3. `cp config.example.json config.json`
4. `sh 安装开机自启.sh`

如果工具目录在桌面、文稿或下载中，需要在 **系统设置 → 隐私与安全性 → 完全磁盘访问权限** 里加入 Python，否则后台进程无权读取该文件夹。

取消：`sh 取消开机自启.sh`

## 命令

在本文件夹中也可以直接运行：

```text
python update_tool_docs.py              # 扫描一次并更新文档
python update_tool_docs.py --watch      # 持续监控（前台）
python update_tool_docs.py --install    # 设置开机自启，并立即在后台启动
python update_tool_docs.py --uninstall  # 取消开机自启，并停止后台进程
python update_tool_docs.py --stop       # 仅停止后台进程
```

Linux / macOS 把 `python` 换成 `python3`。

## 配置

`config.json` 字段：

| 字段 | 含义 |
| --- | --- |
| `use_net` | 是否联网查询 GitHub / 官网 |
| `translate` | 是否翻译英文简介 |
| `baidu_appid` / `baidu_key` | 百度翻译开放平台的 APPID 和密钥。都填了才会翻译；不填则保留原文 |
| `remove_missing` | 文件从文件夹消失后，是否删除表格中对应行 |

翻译请使用[百度翻译开放平台](https://fanyi-api.baidu.com/)的通用文本翻译。不要把填好密钥的 `config.json` 提交到 Git 或发给别人。

## 会识别什么

- 项目文件夹和压缩包（zip / tar）：读取 README、`package.json`、`pyproject.toml`、`.git/config` 等
- Windows `.exe` / `.dll`：产品名、公司、官网（在 Linux / macOS 上也能读）
- macOS `.app`：`Info.plist`
- Linux `.deb`：软件包说明和主页
- `.dmg`、`.AppImage`、`.rpm`、`.pkg`：根据文件名猜测，并尝试搜索官网

识别不出时对应格会填「待补充」。

## 行为说明

- 程序所在文件夹、`工具说明.md`、隐藏文件和下载中的临时文件（`.crdownload`、`.tmp` 等）不会被登记。
- 同一时间只运行一个监控进程。
- 空闲时使用系统原生的目录通知，不轮询文件夹。
- 日志写在本文件夹的 `run.log`。

## 许可证

[MIT](LICENSE)
