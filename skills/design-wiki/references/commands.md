# 运行与命令

Python 3.10+。脚本采用标准库，本机模型HTTPS连接建议安装certifi。无需npm或数据库。
所有工作区路径相对于调用时的当前目录，脚本和随包资产相对于Skill自身定位。

## 启动或复用

将下面的路径替换为Skill的真实目录；此变量只是命令示例，不要求修改系统配置。

```sh
SKILL_DIR=/absolute/path/design-wiki
python3 "$SKILL_DIR/scripts/wiki.py" serve --workspace ./design-wiki-workspace --port 8786
```

服务在前台持续运行；使用可保持进程的终端或执行工具。界面为 http://127.0.0.1:8786 。
状态写入指定工作区的workspace.json，原始响应写入qa_raw/；不会写入Skill的种子数据。
如果要打开已有workspace.json，--workspace填写它的父目录。
若文件名不是workspace.json，直接运行随包 `assets/runtime/server.py --state <原文件> --port <端口>`；不要为了命令示例重命名用户历史。
已有项目在8765运行时，直接将后续命令的--url换为http://127.0.0.1:8765，不另开服务。

```sh
python3 "$SKILL_DIR/scripts/wiki.py" --url http://127.0.0.1:8786 status
python3 "$SKILL_DIR/scripts/wiki.py" --url http://127.0.0.1:8786 rules --output rules-current.json
```

--url是全局参数，放在子命令前。所有JSON输出命令可用--output保存，仍会在标准输出显示内容。--input接受JSON文件，或用“-”读取标准输入。只连接本机地址。

## 具体动作

| 命令 | 输入 | 结果/副作用 |
|---|---|---|
| status | 无 | 运行配置及数量，不读取或返回密钥值 |
| rules / candidates | 无 | 当前规则及哈希 / 候选记录 |
| retrieve | --question，重复--family，--role，--top-k | 仅检索，不调用模型 |
| ask | 同上；--execute-api；可选--request-id、--parent-id | 真实付费请求，立即返回任务记录 |
| wait | --id QA编号；--timeout 0–50 | 查询已有任务，不创建请求；未完成退出码3 |
| history | 无 | 所有问答记录及状态 |
| submit | --input候选JSON | 候选入库，不改变规则 |
| decide | --input裁决JSON | 保存approve/reject，必须有实际依据 |
| preview | --id候选编号 | 仅已批准候选可预览，返回preview_id |
| publish | --id候选编号 --preview-id编号 | 应用已审核本地修订；保留历史 |
| judge | --input含case对象的JSON | 保存一次结构化判断 |
| review-judgment | --id旧判断编号 | 为受影响记录创建新版本 |
| context-preview / context-apply | --input JSON | 项目条件变化及影响，不用于绕过规则审核 |
| check-material / register-material | --input候选登记JSON | 原有入库检查示例接口，见随包ingestion.py；不等于发布规则 |
| export | 可选--output | 导出工作区JSON；原始API响应另存qa_raw/ |

出现HTTP 409，先读错误与最新状态，通常是基准或预览过期；重新检查差异，不自动重放发布。
问答POST无法取得响应时，先根据已打印的request_id查history。相同request_id会返回已有任务，换一个ID重试可能产生额外计费。

## 密钥

DeepSeek优先读取服务进程的DEEPSEEK_API_KEY环境变量。公开版只读取DEEPSEEK_API_KEY环境变量，不读取其他应用的凭证。
不要把密钥粘进聊天、写进payload或Skill。凭证存在不保证可用，真实返回才证明调用成功。
缺少CA时可在用户环境安装certifi；保留TLS校验。连接失败保留错误，不通过关闭证书检查处理。

## 运行核心与来源

assets/runtime/保存本项目2026-11-03版核心、静态界面和公开语料。assets/runtime_manifest.json记录原样复制文件的SHA256。
公开资料及许可说明在assets/runtime/data/公开语料来源与使用说明.txt、data/licenses/。
随包不带运行状态、密钥或先前付费答案。首次启动是新的原始规则工作区。

## 自检

先执行 `python3 scripts/self_test.py`，覆盖脚本与本地API完整流程；付费接口使用明确的测试替身。
如更改运行核心，再执行：

```sh
python3 -B -m unittest discover -s "$SKILL_DIR/assets/runtime/tests" -v
```

公开版核心有43项开发回归。测试不是用户研究或模型准确率评测，不会调用真实付费模型。
