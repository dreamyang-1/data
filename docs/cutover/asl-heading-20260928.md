# ASL 展示标题调整

用户圈定ASL代码块上方“结构化提取”，要求改成“结构化解析与校验”。
仅修改公共渲染函数中的标题为“结构化解析与校验（ASL）：”；上方“解析校验”
主节点名称、ASL协议、SQL、上游结构化提取提示词及工具名称均不改动。
普通、多任务和追问复用同一渲染函数。

清单：app/presentation/intent_recognition.py（1行）；
tests/test_intent_recognition_display.py、tests/test_api.py（各1条标题断言随产品文案更新）。
不改变旧格式传输兼容用例。相关展示/API/解析顺序/Critical共339项通过。
全量回归及发布结果见后续记录。V1纯展示变更，不涉及V2替换与Catalog切流。
