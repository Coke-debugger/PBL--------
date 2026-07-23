# _selftest · 自查脚本参考样例

供学生体验 `validate_submission.py` 两种输出，随附录 D 同包发布。

```
python ../validate_submission.py sample_ok    # 预期: PASS，退出码 0
python ../validate_submission.py sample_bad   # 预期: FAIL ×4 + WARN ×2，退出码 1
```

| 目录 | 内容 |
|---|---|
| `sample_ok/` | 完全合规的一对提交文件（教案含配对公式、内联学习单附录；process.json 含 4 角色、互评引用链、4 条逐条修改说明），可作为格式参照 |
| `sample_bad/` | 故意植入三类典型违约: ① 命名违规（`stu003_样本A_polished.md` 样本ID含中文，NAME-01）② LaTeX 行内 `$` 不配对（`20250102_PHY01_polished.md`，TEX-02）③ process.json Schema 违规（roles 缺 `expertise` 字段 SCH-02、`modifications` 为空 SCH-16）；另演示两类 WARN（无"目标"类标题 STR-03、角色数 <3 SCH-08） |

注意: 样例内容仅为格式示范，其教学内容质量不构成量规评分参照（评分参照见附录 E 公开练习三元组）。
