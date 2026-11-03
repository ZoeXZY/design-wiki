# 检索、作答与验证

## 默认先看证据

```sh
python3 "$SKILL_DIR/scripts/wiki.py" --url http://127.0.0.1:8786 retrieve --question 'Ant Design与GOV.UK对主按钮数量有什么建议？' --family 'Ant Design' --family 'GOV.UK' --role guidance --top-k 4 --output evidence.json
```

可选来源：WCAG、Ant Design、GOV.UK、USWDS、ACT、本地规则。资料角色：normative、guidance、change_log、local_policy；不填则不限。
资料只有固定公开快照；问题超出范围时说明缺失，不从模型常识补成“已检索结论”。Top-K不少于所选来源数，最多8。
检索保留来源章节；GOV.UK历史条目按日期行切分。看清日期与哪一项变更绑定，不能把相邻日期拼上去。

## 两种作答方式不要混淆

- 用户只要资料解释时，当前会话模型可以基于返回证据作答，附原文链接和适用条件；标注这是当前会话作答。这不会自动进入应用问答历史，不声称用了DeepSeek或记录了API用量。
- 用户要实际运行项目、统一参数实验或持久化问答时，使用ask和wait。已有调用授权即可执行，不重复确认。

```sh
python3 "$SKILL_DIR/scripts/wiki.py" --url http://127.0.0.1:8786 ask --question 'GOV.UK何时补充了开始按钮为何使用链接的解释？' --family 'GOV.UK' --role change_log --execute-api --request-id my-run-unique-001 --output request.json
python3 "$SKILL_DIR/scripts/wiki.py" --url http://127.0.0.1:8786 wait --id QA-实际返回编号 --timeout 30 --output answer.json
```

wait退出码3表示仍在运行，继续等待同一编号，不重新提交。失败和中断不自动重试；再次调用应保留原失败记录，并区分新的请求。
重答用同一问题与检索条件，增加--parent-id原QA编号和新的request_id；只有成功重答后原记录才被标记已有后续版本。

## 固定配置与记录

运行时以status和每条record.parameters为准。随包配置是DeepSeek deepseek-flash，thinking.enabled、reasoning_effort=max、max_tokens=8192、stream=false；未设置temperature。
检索为BM25加术语扩展，Embedding模型为无。不要称为已训练模型或向量检索。
每条记录保留原始问题、messages及哈希、检索证据、参数、模型回显、usage和原文答案；响应原文在工作区qa_raw/。
正常响应的completion_tokens包含推理用量。网络失败无usage时费用未知，不估算成零，也不把它计入成功样本。
导出工作区后，交付原始响应时一并复制qa_raw/，只给JSON导出不能代替全部原始响应。

## 核验方式

检查finish_reason、空答案、引用编号，再抽查每条事实是否得到对应片段支持。规则版本、适用范围、例外条件、日期绑定、跨来源比较是本项目的重点。
系统只自动检查[E1]等编号是否属于本次证据，semantic_support_verified=false必须如实解释。

对比试验使用相同资料快照、问题与模型设置，只改变拟研究的检索变量；否则单独标注实验条件差异。保留失败、截断和空答案，不通过改写答案提高成绩。
本项目先前的4条实测是开发验收，40项回归是软件测试；均不是盲测准确率。不要为了展示Skill再重复付费实验，除非本次任务需要。

可用的少量验收情境：
1. 双来源按钮建议：须分别取到两边资料，不把按钮区与整页混为一谈。
2. 变更日期：解释开始按钮为何是链接的记录为Oct 2020；仍应回查实际证据，不把此参考答案发送给模型充当输入证据。
3. 本地规则变更前后：同一问题读取当前本地字段，明确本地政策不等于官方修订。
4. 超出资料的问题：找不到支持时明确缺失，不能编造来源。
