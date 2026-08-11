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
| 一段 Java 代码怎么翻译? | `resolve_java_code("<java代码>")` → 渐进式披露分层检索(见下文) |

## 渐进式披露的分层检索(核心特性)

**把 Java 代码按粒度分层,每层生成自然语言(NL)描述,分别去
仓颉文档检索**。细粒度没有一一对应时,自动上升到粗粒度找等价功能。
在此基础上叠加**两阶段检索**(类型锁定 → 方法匹配),让结果从"候选列表"
变成"一个可直接使用的建议"。

### 分层检索(三层 NL)

```
Level 1  api      最细粒度: 单个 API 调用      "map.put(k, v)"
                  → NL "insert a key-value pair into a map"
                  → 检索: 可能没有 1:1 对应(Cangjie 没有 put)

Level 2  statement 中间粒度: 语句/代码段        "while ((len = in.read(buf)) > 0) {...}"
                  → NL "copy stream data chunk by chunk until EOF"
                  → 检索: 对应 Cangjie 的一个或几个 API

Level 3  function 最粗粒度: 整段函数            "public void copyFile(...) {...}"
                  → NL "copy a file"
                  → 检索: 对应 Cangjie 的整个功能模块
```

### 两阶段检索(类型锁定 → 方法匹配)

`resolve_java_code` 的完整流程:

```
输入: HashMap<String,Integer> map = new HashMap<>(); map.put(k, v);
│
├─ Stage 1 类型锁定(把 3537 条候选缩小到几个类)
│    extract_types() 提取 Java 类型(HashMap, String, Integer)
│    → java_to_cangjie 查映射表(3257 条,含 x2cangjie 类型翻译产物)
│    → search_api 相似度检索 → 候选 Cangjie 类型
│    ★ 关键:类型翻译产物让"Java 类型 → Cangjie 类型"从猜变成查表
│      (java.io.BufferedReader → StringReader 这类映射直接命中)
│
├─ Stage 2 分层 NL 检索 + 交叉验证
│    三级 NL 分别检索,但每个命中若属于锁定类型则加分(type_matched)
│    → 防止把 PrettyPrinter.put 当成 HashMap.put
│
└─ 输出 suggested(可直接使用的建议)
     {cangjie_type: HashMap, module: std.collection,
      members: [add(K,V), replace(K,V), ...], examples: [...]}
```

**工作机制**:

1. `describe_java_code` 把 Java 代码在三级粒度上各生成一条中英双语 NL 描述
   (配置了 LLM 时用 LLM 生成,质量最好;否则用启发式:驼峰拆分 + 术语映射)
2. `resolve_java_code` 先做类型锁定(提取 Java 类型 → 查映射表/相似度检索),
   再把每一级的 NL 描述分别送进 BM25 检索(API 文档 + 示例)
3. 每层算出命中分数(查询词与顶部结果的重叠度),命中属于锁定类型时加分;
   **分数最高的层即为 `best_level`**
4. 返回 `suggested`:从锁定类型里取成员和示例,一次调用拿到完整建议

**实测效果**(deepseek-v4-flash 生成 NL):

| Java 代码 | best_level | suggested |
|---|---|---|
| `map.put(key, value)` | statement | `HashMap @ std.collection`,成员 `add(K,V)`/`replace(K,V)` |
| `reader.readLine()` | statement | `StringReader @ std.io`,成员 `read`/`readToEnd`/`readUntil`/`lines` |
| `while ((len = in.read(buf)) > 0) {...}` | api | `InputStream @ std.io`,成员 `read(Array<Byte>)` |

分层检索让"方法名不同但功能相同"的匹配(put→replace、readLine→readln)从碰运气
变成可检索——因为 NL 描述描述的是**功能意图**而不是方法名;类型锁定又保证了
方法匹配发生在正确的类里,两阶段合起来就是完整的"Java 方法 → Cangjie 方法"解析链。

## 架构

```
cangjie-knowledge-mcp/
├── config.yaml                  # 语料路径、索引参数、LLM 配置
├── src/cjkb/
│   ├── models.py                # ApiRecord / ExampleRecord / JavaMapping 数据模型
│   ├── config.py                # 配置加载(支持环境变量覆盖)
│   ├── java_types.py            # Java 类型提取器(声明/泛型/调用接收者/强转)
│   ├── nl_generator.py          # Java 代码 → 中英双语 NL 描述(API/语句/函数三级)
│   ├── layered_search.py        # 两阶段检索:类型锁定 + 分层 NL + 交叉验证
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
│   ├── import_type_mappings.py  # 导入 x2cangjie 类型翻译产物(3257 条映射)
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
j2cjlib shim + 术语表 ────────┘         │                          (stdio, 9 个工具)
x2cangjie 类型翻译产物 ─────────────────┘
  (import_type_mappings.py, 3257 条映射)  │
                                         │
java 代码 → java_types(提取) → layered_search(类型锁定+渐进披露) ──┘
```

### 数据来源(知识库的内容来自哪里)

| 来源 | 内容 | 位置 |
|---|---|---|
| **CangjieCorpus** | 官方 stdlib 文档:`std.*` 37 个模块 + `stdx.*` 扩展库,含 API 签名、功能说明、官方示例 | `misc/CangjieCorpus`(即 [gitcode.com/Cangjie/cangjie_runtime](https://gitcode.com/Cangjie) 文档的本地镜像) |
| **j2cjlib** | 手写的 Java 兼容 shim 类(`J2CjThread`、`J2CjByteArrayInputStream`、`TimeUnit`…),直接给出 Java 类 → Cangjie 类的对应 | `misc/j2cjlib` |
| **java_cangjie_terms.yaml** | Java 术语 → Cangjie 术语词汇表,用于查询扩展(搜 "Thread" 也能命中含"线程"的文档) | x2cangjie `configs/` |
| **x2cangjie 类型翻译产物** | `translate_type_rag.py` 产出的 **3257 条** Java→Cangjie 类型映射(含 reasoning),让类型锁定从"猜"变"查表" | x2cangjie `data/java/type_resolution/`,用 `scripts/import_type_mappings.py` 导入 |

### 检索原理

1. **分词**:驼峰拆分(`getOrThrow` → `get or throw`)、下划线拆分(`read_file_bytes` → `read file bytes`)、中英文混合。
2. **BM25 字段加权**:`name × 4` > `signature × 3` > `module × 2` > `tags × 1.5` > `description × 1`,让"按名检索"比"按描述检索"更准。
3. **Java 术语扩展**:查询 token 先查 Java→Cangjie 映射表,把 Java 词汇展开成 Cangjie 同义词再检索(解决 `Thread` vs `线程` 的匹配问题)。
4. **精确名索引**:`get_api_details` / `get_class_members` 走精确名 → 记录索引,不依赖相似度。

### MCP 工具(9 个)

| 工具 | 说明 | 典型调用 |
|---|---|---|
| `search_api` | API 相似度检索,返回签名/模块/来源/描述 | `search_api("HashMap put key value")` |
| `get_api_details` | 按名精确查函数/类/接口 | `get_api_details("add", module="std.collection")` |
| `get_class_members` | 类的全部成员(init/prop/func) | `get_class_members("ArrayList")` |
| `find_examples` | 检索示例代码 | `find_examples("read file lines")` |
| `java_to_cangjie` | Java 符号 → Cangjie 等价物 | `java_to_cangjie("java.util.List")` |
| `error_fix_hint` | 编译错误 → 相关 API + 示例 | `error_fix_hint("cannot find symbol println")` |
| `list_modules` | 列出知识库中所有模块 | `list_modules()` |
| `resolve_java_code` | **渐进式披露分层检索**:Java 代码 → 三级 NL 描述分别检索,返回各层结果 + `best_level` | `resolve_java_code("map.put(key, value);")` |
| `describe_java_code` | 只生成 Java 代码的中英双语 NL 描述(不检索),供构建 prompt 用 | `describe_java_code("String line = reader.readLine();")` |

## 快速开始

**知识库数据(JSONL)已随仓库托管在 GitHub**——clone 即用,不需要在本机重新构建。
BM25 索引(.pkl)是派生产物,首次运行时自动重建。

```bash
# 1. clone + 安装依赖(仅 PyYAML;索引核心零依赖)
git clone https://github.com/sskacc/cangjie-knowledge-mcp.git
pip install -r requirements.txt

# 2. 一键就绪:校验数据 + 自动重建索引(首次运行会自动完成,可跳过)
python scripts/install_kb.py

# 3. 命令行试一下
python scripts/query_demo.py "HashMap put key value"
python scripts/query_demo.py --class-members ArrayList
python scripts/query_demo.py --java "java.util.List"

# 4. 启动 MCP 服务器(stdio)
python -m cjkb.mcp_server --data-dir data
```

**首次启动 MCP 服务器时**,如果 `data/` 里只有 JSONL 没有 .pkl(刚 clone 的状态),
`Searcher.load` 会自动重建 BM25 索引(约 1 秒),无需手动干预。

### 手动维护知识库(推荐工作流)

你打算手动维护 `data/`——这正是 JSONL 托管进 git 的原因:**JSONL 是人类可读、
可 diff、可 review 的源数据**,pkl 是机器生成的派生品。维护流程:

```bash
# 1. 编辑 data/*.jsonl(增删 API 记录 / 示例 / Java 映射)
#    手动维护时建议直接编辑 jsonl,不重跑 build_kb.py(避免覆盖你的改动)

# 2. 重建索引(改了 jsonl 后必须做,否则检索用旧索引)
python scripts/install_kb.py    # 或直接启动 MCP,load 时检测到 jsonl 更新会自动重建

# 3. 提交推送
git add data/*.jsonl
git commit -m "kb: add HashMap add/replace examples"
git push
```

> 规则:**JSONL 是数据,进 git;pkl 是派生品,不进 git**。新机器 clone 后
> install_kb.py / MCP 首启都会自动重建 pkl,无需手动处理。

### 从零重建(只在语料变化时用)

如果 Cangjie 官方语料更新了,或你想重新收集:

```bash
python scripts/build_kb.py \
  --corpus D:/x2cangjie/x2cangjie/misc/CangjieCorpus \
  --j2cjlib D:/x2cangjie/x2cangjie/misc/j2cjlib \
  --terms D:/x2cangjie/x2cangjie/configs/java_cangjie_terms.yaml

# 重建后导入 x2cangjie 类型翻译产物(3257 条映射,类型锁定靠它)
python scripts/import_type_mappings.py \
  --type-resolution D:/x2cangjie/x2cangjie/data/java/type_resolution
```

> ⚠️ `build_kb.py` 会**覆盖** data/*.jsonl。如果你手动维护过 jsonl,
> 重跑前先 `git commit`(可回滚),或把手动改动备份。

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

注册后,agent 在片段翻译和错误修复时可以直接调用上述 9 个工具。

## 在片段翻译流程中使用

以 x2cangjie 的 `translate_fragment.sh` 流程为例,推荐的调用时机:

1. **翻译前**(对应 prompt 注入顺序中的 RAG 层,见 `docs/fragment_translation_enhancements.md`):
   - 把整个待翻译片段丢给 `resolve_java_code` → 得到三级检索结果 + `best_level`,
     从最佳层级取 API 签名和示例注入 prompt
   - 细节确认:`get_class_members("HashMap")` → 确认实际方法名(`add` 而非 `put`);
     `java_to_cangjie("java.util.HashMap")` → 得到 Cangjie 类型
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

## LLM 配置(可选,用于两处)

配置 `config.yaml` 的 `llm` 段或环境变量 `OPENAI_API_KEY` / `OPENAI_BASE_URL` /
`OPENAI_MODEL`。LLM 用于两处:

1. **NL 描述生成**(`resolve_java_code` / `describe_java_code`):LLM 把 Java 代码转成
   中英双语 NL 描述,比启发式(驼峰拆分)质量高很多。**未配置时自动退回启发式**,
   检索功能不受影响,只是 NL 描述质量较低。
2. **补写缺失示例**(见下):为没有官方示例的 API 生成示例。

```bash
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"   # 可选
export OPENAI_MODEL="gpt-4o-mini"                        # 可选
```

## 可选:LLM 生成缺失示例

知识库中**没有官方示例**的 API,可以用 LLM 自动补写(每条标记 `generated=true`,
与官方示例区分)。用独立脚本 `scripts/generate_examples.py`,直接作用于
`data/` 现有知识库,**不重新收集语料、不覆盖手动维护**:

```bash
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"   # 可选
export OPENAI_MODEL="gpt-4o-mini"                        # 可选

# 先看有多少 API 缺示例
python scripts/generate_examples.py --dry-run
#   → "APIs missing an example: 1517"

# 生成前 50 条(断点续跑:自动跳过已生成的)
python scripts/generate_examples.py --limit 50

# 生成全部缺失的(1517 条,耗时较长,可分多次跑)
python scripts/generate_examples.py --limit 0

# 多次运行即可补全;生成的示例写入 data/examples.jsonl,generated=true
```

特点:
- **断点续跑**:每次自动跳过已生成的 title,重跑不会重复生成
- **优先级**:类/接口/枚举优先(价值最高),签名长的函数次之
- **失败容忍**:LLM 空响应自动重试 2 次,失败的不计入,下次重跑会补
- **自动重建索引**:追加后重建 BM25,`find_examples` 立即能搜到新示例
- 生成后记得 `git add data/examples.jsonl && git commit` 把维护成果提交

## 测试

```bash
python -m unittest discover -s tests -v   # 单元测试
python tests/mcp_e2e.py                   # MCP stdio 端到端测试(7 个基础工具)
python tests/mcp_layered_e2e.py           # 分层检索端到端测试(启发式,无需 LLM)
python tests/mcp_layered_llm.py           # 分层检索端到端测试(真实 LLM,需配 key)
```

## 知识库现状(基于本机语料构建)

```
apis:           3537  (std.* + stdx.* + manual 中可解析的函数/类/接口/枚举)
examples:        234  (228 官方 + 6 LLM 生成;另有 1517 个 API 待补写示例)
java_mappings:  3257  (93 j2cjlib+术语表 + 3164 x2cangjie 类型翻译产物)
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
