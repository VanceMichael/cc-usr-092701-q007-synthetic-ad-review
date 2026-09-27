# 数字人健康广告审查

本项目保存数字人健康广告审查所需的领域上下文和校验契约，便于服务端功能围绕真实业务参与方展开。当前版本只提供资料读取、结构校验和命令行摘要，数据均为演示用虚构内容。

## 参与方

平台审核员、广告主、制作机构、法务人员

## 事实资料

- 报道指出原生AI数字人虚构使用体验可能构成虚假广告
- 药品、医疗器械广告不得利用广告代言人推荐证明
- 责任需要穿透到广告主、发布者、制作机构和实际获利主体

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 编译

```bash
python3 -m compileall -q src tests
```

## 命令行检查

```bash
python3 -m src.synthetic_ad_review.context fixtures/context.json
```
