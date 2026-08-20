每个flow一个sandbox

每个flow包含多个turn，每个turn一个独立的配置

每次turn包含多次interaction

user_simulator：每个turn一个新的

llm_judger：每个interaction独立一个新的

行为：

turn起始：根据turn配置文件，进行workspace更新。

第一次interaction开始：本轮配置文件中记录的问题为request，发送给待测模型。

每次interaction结束：将reponse和沙箱状态发送给llm_judge，得到分数。

若分数高于阈值，本轮结束，此时分数为本turn分数。

否则，进行下一次interaction（有次数上限）

非第一次的interaction开始：使用  user_simulator ，根据本轮turn的上下文，以及<u>llm_judge的错误原因</u>得到新的request，发送给被测模型。
