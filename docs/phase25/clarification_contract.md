# 澄清决策契约

`ClarificationDecision` 记录错误类型、阻塞路径、候选、信息增益、已问槽位、回退/默认可用性以及 task/state 版本。只有 `USER_AMBIGUITY` 可以创建 Pending。模型漏抽、ASL 漏列、SQL 计划失败、执行失败和结果列缺失均为系统责任，不得改写为“请用户补充”。一次默认只问一个最高信息增益问题；同槽不重复问；新任务可替换 Pending；取消清除 Pending；“按默认”只有登记策略存在时才允许。
