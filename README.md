# cangjie-knowledge-mcp

Cangjie 知识库 + MCP 服务器,为 Java → Cangjie 片段翻译提供 API 检索能力。

在翻译一个 Java 片段之前,先通过 MCP 工具做相似度检索,找到片段中用到的类/方法/类型在
Cangjie 标准库中的**来源(哪个库、哪个模块)**、**完整签名**、**官方示例代码**;翻译失败进入
错误修复循环时,也可以调用本工具(`error_fix_hint`)根据编译错误定位相关 API 和示例。

## 它能回答什么问题

| 场景 | 用法 |
|---|---|
| `HashMap.put(key, value)` 在 Cangjie 里怎么调? | `search_api("map put key value")` → 找到 `HashMap` 的 `add(K,V)`/`replace(K,V)`(Cangjie 没有 `put`) |
| `ArrayList` 有哪些方法? | `get_class_members("ArrayList")` → 27 个成员及签名 |
| 读文件怎么写? | `find_examples("read file bytes")` → 官方 sample 代码 |
| `java.util.List` 对应 Cangjie 什么? | `java_to_cangjie("java.util.List")` → j2cjlib 映射 |
| `cannot find symbol println` 怎么修? | `error_fix_hint("...")` → 相关 API 文档 + 示例 |

## 架构

```
cangjie-knowledge-mcp/
├── config.yaml                  # 语料路径、索引参数、LLM 配置
├── src/cjkb/
│   ├── models.py                # ApiRecord / ExampleRecord / JavaMapping 数据模型
│   ├── config.py                # 配置加载(支持环境变量覆盖)
│   ├── collector/
│   │   ├── corpus_parser.py     # 解析 CangjieCorpus 官方文档 → API/示例记录
│   │   ├── j2cj_parser.py       # 解析 j2cjlib shim + 术语表 → Java→Cangjie 映射
│   │   └── example_writer.py    # (可选)LLM 为缺少示例的 API 生成示例
│   ├── index/
│   │   ├── bm25.py              # 纯标准库 BM25(字段加权, 驼峰/下划线分词)
│   │   └── searcher.py          # 检索 API(相似度 + 精确名 + Java 术语扩展)
│   └── mcp_server.py            # MCP stdio 服务器(零第三方依赖)
├── scripts/
│   ├── build_kb.py              # 收集 + 建索引 → data/
│   └── query_demo.py            # 命令行检索演示
├── tests/                       # 单元测试 + MCP 端到端测试
└── data/                        # 构建产物(知识库, gitignore)
```

### 数据流

```
CangjieCorpus(官方文档)
   │  libs/std/*  API 参考(3543 条 API)
   │  *_package_samples/*  官方示例
   │  manual/ 语言手册
   v
collector/corpus_parser.py ──┐
                             ├──> KnowledgeBase(JSONL) ──> BM25 索引 ──> MCP server
j2cjlib shim + 术语表 ────────┘         │                          (stdio, 7 个工具)
```

### 数据来源(知识库的内容来自哪里)

| 来源 | 内容 | 位置 |
|---|---|---|
| **CangjieCorpus** | 官方 stdlib 文档:`std.*` 37 个模块 + `stdx.*` 扩展库,含 API 签名、功能说明、官方示例 | `misc/CangjieCorpus`(即 [gitcode.com/Cangjie/cangjie_runtime](https://gitcode.com/Cangjie) 文档的本地镜像) |
| **j2cjlib** | 手写的 Java 兼容 shim 类(`J2CjThread`、`J2CjByteArrayInputStream`、`TimeUnit`…),直接给出 Java 类 → Cangjie 类的对应 | `misc/j2cjlib` |
| **java_cangjie_terms.yaml** | Java 术语 → Cangjie 术语词汇表,用于查询扩展(搜 "Thread" 也能命中含"线程"的文档) | x2cangjie `configs/` |

### 检索原理

1. **分词**:驼峰拆分(`getOrThrow` → `get or throw`)、下划线拆分(`read_file_bytes` → `read file bytes`)、中英文混合。
2. **BM25 字段加权**:`name × 4` > `signature × 3` > `module × 2` > `tags × 1.5` > `description × 1`,让"按名检索"比"按描述检索"更准。
3. **Java 术语扩展**:查询 token 先查 Java→Cangjie 映射表,把 Java 词汇展开成 Cangjie 同义词再检索(解决 `Thread` vs `线程` 的匹配问题)。
4. **精确名索引**:`get_api_details` / `get_class_members` 走精确名 → 记录索引,不依赖相似度。

### MCP 工具(7 个)

| 工具 | 说明 | 典型调用 |
|---|---|---|
| `search_api` | API 相似度检索,返回签名/模块/来源/描述 | `search_api("HashMap put key value")` |
| `get_api_details` | 按名精确查函数/类/接口 | `get_api_details("add", module="std.collection")` |
| `get_class_members` | 类的全部成员(init/prop/func) | `get_class_members("ArrayList")` |
| `find_examples` | 检索示例代码 | `find_examples("read file lines")` |
| `java_to_cangjie` | Java 符号 → Cangjie 等价物 | `java_to_cangjie("java.util.List")` |
| `error_fix_hint` | 编译错误 → 相关 API + 示例 | `error_fix_hint("cannot find symbol println")` |
| `list_modules` | 列出知识库中所有模块 | `list_modules()` |

## 快速开始

```bash
# 1. 安装依赖(仅 PyYAML;索引核心零依赖)
pip install -r requirements.txt

# 2. 构建知识库(默认读 config.yaml 中的语料路径)
python scripts/build_kb.py

# 3. 命令行试一下
python scripts/query_demo.py "HashMap put key value"
python scripts/query_demo.py --class-members ArrayList
python scripts/query_demo.py --java "java.util.List"

# 4. 启动 MCP 服务器(stdio)
python -m cjkb.mcp_server --data-dir data
```

如果语料不在默认路径,用参数覆盖:

```bash
python scripts/build_kb.py \
  --corpus D:/x2cangjie/x2cangjie/misc/CangjieCorpus \
  --j2cjlib D:/x2cangjie/x2cangjie/misc/j2cjlib \
  --terms D:/x2cangjie/x2cangjie/configs/java_cangjie_terms.yaml
```

### 在 agent 工具中注册 MCP

在 opencode 的 `opencode.json`(或 Claude Desktop 配置)中注册:

```jsonc
// opencode.json
{
  "mcp": {
    "cangjie-kb": {
      "type": "stdio",
      "command": "python",
      "args": ["D:/x2cangjie/cangjie-knowledge-mcp/src/cjkb/mcp_server.py", "--data-dir", "D:/x2cangjie/cangjie-knowledge-mcp/data"],
      "env": {}
    }
  }
}
```

注册后,agent 在片段翻译和错误修复时可以直接调用上述 7 个工具。

## 在片段翻译流程中使用

以 x2cangjie 的 `translate_fragment.sh` 流程为例,推荐的调用时机:

1. **翻译前**(对应 prompt 注入顺序中的 RAG 层,见 `docs/fragment_translation_enhancements.md`):
   - 从待翻译片段中提取方法调用/类型引用(如 `HashMap.put`、`FileInputStream`)
   - `java_to_cangjie("java.util.HashMap")` → 得到 Cangjie 类型
   - `get_class_members("HashMap")` → 确认实际方法名(`add` 而非 `put`)
   - `find_examples("HashMap")` → 取官方示例注入 prompt

2. **错误修复循环**(`compositional_translation_validation.py` 中 `cjpm build` 失败后):
   - 把编译错误文本传给 `error_fix_hint` → 返回相关 API 签名和示例
   - 把结果拼进错误反馈 prompt,再让 LLM 修复

3. **类型解析**(`translate_type_rag.py` 的 RAG 兜底层):把 `search_api` 结果作为额外证据注入类型映射 prompt。

> 说明:本项目是**独立于 x2cangjie** 的新项目,不修改 x2cangjie 的代码。接入方式有
> 两种:(a) 在 agent(MCP 客户端)侧注册上面的 server,让 agent 直接调用;
> (b) 在 x2cangjie 的 Python 代码中 import `cjkb` 直接调用 `Searcher`(程序化 API)。

### 程序化调用(不经过 MCP)

```python
import sys
sys.path.insert(0, "src")
from cjkb.config import load_config
from cjkb.index.searcher import Searcher

cfg = load_config("config.yaml")
s = Searcher.load(cfg["output"]["data_dir"], cfg)

for r in s.search_api("HashMap put key value"):
    print(r.name, r.module, r.signature)

for m in s.java_to_cangjie("java.util.List"):
    print(m.java_symbol, "->", m.cangjie_symbol)
```

## 可选:LLM 生成缺失示例

对知识库中**没有官方示例**的 API,可以用 LLM 自动补写示例(每条都标记 `generated=true`,
与官方示例区分):

```bash
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"   # 可选
python scripts/build_kb.py --write-examples --example-limit 50
```

生成的示例会写入 `data/examples.jsonl`,`generated` 字段为 `true`。

## 测试

```bash
python -m unittest discover -s tests -v   # 单元测试
python tests/mcp_e2e.py                   # MCP stdio 端到端测试
```

## 知识库现状(基于本机语料构建)

```
apis:           3543  (std.* + stdx.* + manual 中可解析的函数/类/接口/枚举)
examples:        224  (官方 _package_samples + extra 指南代码块)
java_mappings:    93  (j2cjlib shim 类 + 术语表)
modules:          48  (std: 37, stdx: 11)
```

## 扩展方向

- **向量检索**:当前 BM25 对同义改写不敏感,可加 embedding 层(如 `sentence-transformers`)
  做混合检索。
- **gitcode 官方 API 文档**:gitcode 需要登录才能拉取 `cangjie_runtime/stdlib/doc`,
  CangjieCorpus 已含等价内容;若以后能免密拉取,可把 `stdlib/doc` 的 .md 直接喂给
  `corpus_parser`。
- **翻译记忆库**:把 x2cangjie 历史成功翻译的"Java 片段 → Cangjie 片段"对收进示例库,
  作为 few-shot 参考(类似 Progressive KB,但走 MCP)。
- **错误模式库**:把 `analyze_errors.py` 的历史错误分类结果沉淀为"错误 → 修复模式"条目。
