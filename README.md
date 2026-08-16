# cangjie-knowledge-mcp

Cangjie 知识库 + MCP 服务器,为 Java → Cangjie 片段翻译提供 API 检索能力。

在翻译一个 Java 片段之前,先通过 MCP 工具做检索,找到片段中用到的类/方法/类型在
Cangjie 标准库中的**来源(哪个库、哪个模块)**、**完整签名**、**官方示例代码**;翻译失败
进入错误修复循环时,也可以调用本工具(`error_fix_hint`)根据编译错误定位相关 API 和示例。

## 它能回答什么问题

| 场景 | 用法 |
|---|---|
| `HashMap.put(key, value)` 在 Cangjie 里怎么调? | `search_api("map put key value")` → 找到 `HashMap` 的 `add(K,V)`/`replace(K,V)`(Cangjie 没有 `put`) |
| `ArrayList` 有哪些方法? | `get_class_members("ArrayList")` → 成员及签名 |
| 读文件怎么写? | `find_examples("read file bytes")` → 官方 sample 代码 |
| `java.util.List` 对应 Cangjie 什么? | `java_to_cangjie("java.util.List")` → j2cjlib 映射 |
| `cannot find symbol println` 怎么修? | `error_fix_hint("...")` → 相关 API 文档 + 示例 |
| 一段 Java 代码怎么翻译? | `resolve_java_code("<java代码>")` → per-call 建议(见下文) |

## 核心特性:per-call 渐进式披露检索

**对代码块中的每个 API 调用独立检索,每个调用产出一个 suggest**——包括**构造调用**
(`new Xxx(...)`,每个构造也单独建议,含 `new HashMap<>()` 菱形泛型)。细粒度(api)
没有一一对应时,自动上升到语句级(statement),再不行才为整个代码块生成块级
suggest。在此之上叠加**两阶段检索**(类型锁定 → 方法匹配)+ **语义 rerank**,
让结果从"候选列表"变成"一组可直接使用的建议"。

### 三层粒度

```
Level 1  api       最细粒度: 单个 API 调用      "map.put(k, v)"
                   → 锁 receiver 类型(HashMap) → 该类中与调用意图最匹配的成员
                   → 产出该调用的 suggest

Level 2  statement 中间粒度: 整行语句          "map.put(k, v);"
                   → NL 检索,对应 Cangjie 的一个或几个 API
                   → 用于 receiver 类型不可锁定时的降级

Level 3  function  最粗粒度: 整个代码块         "public void copyFile(...) {...}"
                   → NL "copy a file",检索整个功能模块
                   → 只生成一个块级 suggest(兜底)
```

### per-call 工作流

`resolve_java_code` 的完整流程:

```
输入: BufferedReader reader = new BufferedReader(new InputStreamReader(System.in));
      String line = reader.readLine();
│
├─ extract_types() 拆出每个调用(含构造调用 `new Xxx(...)` 与 `new HashMap<>()`)
│    calls = [reader.readLine, new BufferedReader, new InputStreamReader, ...]
│
├─ 对每个 call 独立解析:
│    ┌─ L1 api: 锁 receiver 类型(查 x2cangjie 映射表 / BM25 类名兜底)
│    │    → 该类成员按该调用的 NL 意图重排(含 rerank)→ 返回 top-k 成员
│    │    (map.put → HashMap 的 add/contains/...;reader.readLine → StringReader 的 readln/...
│    │     new BufferedReader → StringReader 的 init(T)/lines/... ← 构造调用单独建议)
│    ├─ L2 statement: receiver 类型无法锁定(如 System.out.println)时,
│    │    用整行语句 NL 检索 → 返回候选 API 列表
│    └─ L3 function: 整个块仍无法解析时,生成一个块级 suggest(兜底)
│
└─ 输出 suggestions[]: 每个调用一个建议
```

**关键点**:

1. **每个 API 调用都有自己的 suggest**——`map.put` 和 `reader.readLine` 是不同类型
   的不同意图,各自锁各自的类、返回各自最匹配的成员。**构造调用也单独建议**。
2. **api 级只返回 top-k 成员**(默认 5)而非整个类;粗粒度(statement/function)才返回完整列表。
3. **升降级规则**:receiver 类型可锁定(含构造调用的类名)→ api;不可锁定 → statement;
   整块都无法解析 → function。
4. **类型锁定**:查表(精确)+ BM25 类名兜底(相似度),泛型会被剥掉(`HashMap<String,Integer>` → `HashMap`)。

### 语义 rerank(可选增强)

在 BM25 召回之后,用一个**可选的 LLM rerank 层**对 top-k 候选做语义重排,纠正
BM25 在"词形不同但语义相同"场景下的盲区(如 Java 的 `readLine` → Cangjie 的
`readln`,`readln` 词法上拆不出 "read"+"line",BM25 会打 0 分,但 rerank 能理解
"读下一行"就是 `readln`)。思路来自 LongCodeZip 论文(条件困惑度排序比词法相似度
高 7.89%),实现上用 LLM 直接输出候选排序(避免依赖 token 级 log-prob)。

- **recall-rerank 两段式**:BM25 先召回 top_k×3(粗筛),LLM 只精排几十个候选。
- **零依赖回退**:无 `api_key` 或 `rerank=false` 时,顺序与纯 BM25 完全一致。
- **容错**:LLM 超时/报错/输出不可解析,一律回退 BM25 顺序,检索永不因 LLM 失败而退化。

## 架构

```
cangjie-knowledge-mcp/
├── config.yaml                  # 语料路径、索引参数、LLM 配置(api_key 走环境变量)
├── Dockerfile                   # MCP server 容器化镜像
├── opencode.json                # opencode MCP 注册配置
├── src/cjkb/
│   ├── models.py                # ApiRecord / ExampleRecord / JavaMapping 数据模型
│   ├── config.py                # 配置加载(支持环境变量覆盖)
│   ├── java_types.py            # Java 类型提取器(声明/泛型/调用接收者/构造调用/强转)
│   ├── nl_generator.py          # Java 代码 → 中英双语 NL 描述(LLM 或启发式)
│   ├── layered_search.py        # per-call 解析:类型锁定 + 分层 NL + 升降级 → suggestions[]
│   ├── reranker.py              # (可选)LLM 语义 rerank 层
│   ├── collector/
│   │   ├── corpus_parser.py     # 解析 CangjieCorpus 官方文档 → API/示例记录
│   │   ├── j2cj_parser.py       # 解析 j2cjlib shim + 术语表 → Java→Cangjie 映射
│   │   └── example_writer.py    # (可选)LLM 为缺少示例的 API 生成示例
│   ├── index/
│   │   ├── bm25.py              # 纯标准库 BM25(字段加权, 驼峰/下划线/中文 unigram 分词)
│   │   └── searcher.py          # 检索 API(相似度 + 精确名 + Java 术语扩展)
│   └── mcp_server.py            # MCP stdio 服务器(零第三方依赖)
├── scripts/
│   ├── build_kb.py              # 收集 + 建索引 → data/
│   ├── import_type_mappings.py  # 导入 x2cangjie 类型翻译产物
│   ├── install_kb.py            # 一键就绪:校验数据 + 自动重建索引
│   ├── generate_examples.py     # (可选)LLM 补写缺失示例
│   └── query_demo.py            # 命令行检索演示
├── tests/                       # 单元测试 + 综合端到端测试
└── data/                        # 知识库(JSONL 入库, pkl 派生不入库)
```

### 数据流

```
CangjieCorpus(官方文档)   x2cangjie 类型翻译产物   j2cjlib shim + 术语表
        │                        │                      │
        v                        v                      v
  corpus_parser.py        import_type_mappings.py   j2cj_parser.py
        └────────────────────────┼──────────────────────┘
                                 v
                        KnowledgeBase(JSONL) ──> BM25 索引 ──> MCP server
                                 │                              (stdio, 9 个工具)
                                 v
               Java 代码 → extract_types → resolve_java_code(per-call)
```

### 检索原理

1. **分词**:驼峰拆分(`getOrThrow` → `get or throw`)、下划线拆分(`read_file_bytes` →
   `read file bytes`)、**中文 unigram 单字切分**(中文无空格词边界,单字切分让 BM25
   能对中文查询/描述打分,无需引入分词器)。
2. **BM25 字段加权**:`name × 4` > `signature × 3` > `module × 2` > `tags × 1.5` >
   `description × 1`,让"按名检索"比"按描述检索"更准。
3. **Java 术语扩展**:查询 token 先查 Java→Cangjie 映射表,把 Java 词汇展开成
   Cangjie 同义词再检索(解决 `Thread` vs `线程` 的匹配问题)。
4. **精确名索引**:`get_api_details` / `get_class_members` 走精确名索引,不依赖相似度。

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
| `resolve_java_code` | **per-call 渐进式披露**:拆成每个 API 调用,逐个锁类型+分层检索,返回 `suggestions[]` | `resolve_java_code("map.put(key, value);")` |
| `describe_java_code` | 只生成 Java 代码的中英双语 NL 描述(不检索) | `describe_java_code("String line = reader.readLine();")` |

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

### Docker 方式(无需本机 Python)

宿主机没有 Python 时,用 Docker 镜像运行:

```bash
# 构建镜像(打包源码 + 知识库 + PyYAML)
docker build -t cangjie-knowledge-mcp .

# 直接运行 MCP server(stdio)
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test"}}}' \
  | docker run -i --rm cangjie-knowledge-mcp
```

### 在 opencode 中注册 MCP

项目根目录已附带 `opencode.json`(opencode 会自动加载项目级配置),它通过 Docker
镜像注册本 MCP server:

```jsonc
// opencode.json(已随仓库提供)
{
  "mcp": {
    "cangjie-knowledge": {
      "type": "local",
      "command": ["docker", "run", "-i", "--rm", "cangjie-knowledge-mcp"],
      "enabled": true
    }
  }
}
```

若本机有 Python(无需 Docker),可改为:

```jsonc
{
  "mcp": {
    "cangjie-knowledge": {
      "type": "local",
      "command": ["python", "-m", "cjkb.mcp_server"],
      "enabled": true
    }
  }
}
```

注册后,agent 在片段翻译和错误修复时可以直接调用上述 9 个工具。

### 手动维护知识库(推荐工作流)

`data/` 的 JSONL 托管进 git 的原因:**JSONL 是人类可读、可 diff、可 review 的
源数据**,pkl 是机器生成的派生品。维护流程:

```bash
# 1. 编辑 data/*.jsonl(增删 API 记录 / 示例 / Java 映射)
#    直接编辑 jsonl 即可,不必重跑 build_kb.py(重跑会覆盖手动改动)

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

当 Cangjie 官方语料更新,或需要重新收集时。语料来自 **x2cangjie 项目**
(一个 Java→Cangjie 翻译流水线,本知识库是它的配套检索工具),路径按实际
clone 位置填写:

```bash
python scripts/build_kb.py \
  --corpus <x2cangjie路径>/misc/CangjieCorpus \
  --j2cjlib <x2cangjie路径>/misc/j2cjlib \
  --terms <x2cangjie路径>/configs/java_cangjie_terms.yaml

# 重建后导入 x2cangjie 类型翻译产物(类型锁定靠它)
python scripts/import_type_mappings.py \
  --type-resolution <x2cangjie路径>/data/java/type_resolution
```

> ⚠️ `build_kb.py` 会**覆盖** data/*.jsonl。若 jsonl 已有手动改动,
> 重跑前先 `git commit`(可回滚),或先备份手动改动。

## 在片段翻译流程中使用

以 x2cangjie 的翻译流程为例,推荐的调用时机:

1. **翻译前**:把待翻译片段丢给 `resolve_java_code` → 得到每个 API 调用的建议,
   取 API 签名和示例注入 prompt;细节确认用 `get_class_members` /
   `java_to_cangjie` / `find_examples`。
2. **错误修复循环**(`cjpm build` 失败后):把编译错误传给 `error_fix_hint` →
   返回相关 API 签名和示例,拼进错误反馈 prompt。
3. **类型解析 RAG 兜底**:把 `search_api` 结果作为额外证据注入类型映射 prompt。

> 说明:本项目是**独立于 x2cangjie** 的新项目,不修改 x2cangjie 的代码。接入方式:
> (a) 在 agent(MCP 客户端)侧注册上面的 server,让 agent 直接调用;
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

## LLM 配置(可选,用于三处)

配置 `config.yaml` 的 `llm` 段或环境变量 `OPENAI_API_KEY` / `OPENAI_BASE_URL` /
`OPENAI_MODEL`。LLM 用于三处:

1. **NL 描述生成**(`resolve_java_code` / `describe_java_code`):LLM 把 Java 代码转成
   中英双语 NL 描述。**未配置时自动退回启发式**(驼峰拆分 + 术语映射),检索不受影响。
2. **语义 rerank**(`search_api` / `resolve_java_code` 等检索出口):LLM 对 BM25 top-k
   重排。**未配置或 `rerank=false` 时退回纯 BM25 顺序**。
3. **补写缺失示例**:为没有官方示例的 API 生成示例。

```bash
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://api.deepseek.com"    # 可选
export OPENAI_MODEL="deepseek-v4-flash"               # 可选
```

## 可选:LLM 生成缺失示例

知识库中**没有官方示例**的 API,可以用 LLM 自动补写(每条标记 `generated=true`,
与官方示例区分)。用独立脚本 `scripts/generate_examples.py`:

```bash
export OPENAI_API_KEY="..."

python scripts/generate_examples.py --dry-run      # 看有多少 API 缺示例
python scripts/generate_examples.py --limit 50     # 生成前 50 条(断点续跑)
python scripts/generate_examples.py --limit 0      # 生成全部缺失的
```

特点:
- **断点续跑**:自动跳过已生成的 title,重跑不重复
- **失败容忍**:LLM 空响应自动重试 2 次
- **自动重建索引**:生成后立即更新 BM25 索引

## 测试

```bash
# 单元测试 + 综合端到端测试(需 Docker,或本机 Python + PyYAML)
docker run --rm -v "$(pwd):/app" -w /app cjkb-test:latest python -m pytest -q
# 或本机:
pip install pytest
python -m pytest -q
```

测试覆盖:tokenize(含中文 unigram)、BM25、parser、searcher、MCP 协议、NL 生成、
类型提取(含构造调用/菱形泛型)、per-call 分层检索、降级路径、rerank 回退与解析、
9 个工具端到端、错误处理。
