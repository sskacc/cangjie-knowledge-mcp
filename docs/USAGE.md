# cangjie-knowledge-mcp 使用说明

> 本文档讲清楚三件事:**MCP 怎么用**、**每个文件干什么**(含 `data/`)、**数据是怎么流转的**。

---

## 1. 整体架构

```
┌─────────────────────── 构建阶段(一次性/增量) ───────────────────────┐
│                                                                      │
│  CangjieCorpus 官方文档 (libs/std/*, stdx/*, manual/, extra/)        │
│  j2cjlib shim (Java兼容类)        ──►  scripts/build_kb.py ──► data/ │
│  java_cangjie_terms.yaml 术语表                                       │
│  (可选) LLM 补写缺失示例 (--write-examples)                           │
└──────────────────────────────────────────────────────────────────────┘
                                    │ 读 data/(JSONL + BM25 pkl)
                                    ▼
┌─────────────────────── 服务阶段 ─────────────────────────────────────┐
│                                                                      │
│  MCP 服务器 (src/cjkb/mcp_server.py, stdio 协议)                     │
│     ├─ search_api          ├─ java_to_cangjie                        │
│     ├─ get_api_details     ├─ error_fix_hint                         │
│     ├─ get_class_members   └─ list_modules                           │
│     └─ find_examples                                                 │
│         ▲                                                            │
│         │ JSON-RPC over stdin/stdout                                 │
│  ┌──────┴───────┐    ┌──────────────┐    ┌──────────────────┐        │
│  │ opencode     │    │ Claude       │    │ 任意 MCP 客户端   │        │
│  │ (opencode.json)  │ Desktop      │    │ (Cursor 等)      │        │
│  └──────────────┘    └──────────────┘    └──────────────────┘        │
└──────────────────────────────────────────────────────────────────────┘
```

两个阶段互不干扰:**构建阶段**产生知识库文件,**服务阶段**只读这些文件提供服务。
所以知识库建好后,可以只部署服务端,不再需要语料目录。

---

## 2. MCP 怎么用

### 2.1 一次完整的调用流程

```text
客户端 (agent)                         MCP 服务器
    │  1. initialize                     │
    ├───────────────────────────────────►│  返回协议版本 + serverInfo
    │  2. notifications/initialized      │
    ├───────────────────────────────────►│
    │  3. tools/list                     │
    ├───────────────────────────────────►│  返回 9 个工具的定义
    │  4. tools/call {search_api, ...}   │
    ├───────────────────────────────────►│  执行检索,返回 JSON 结果
    │◄───────────────────────────────────┤
```

协议是 JSON-RPC 2.0,走 **stdio**(每行一条 JSON)。不需要 HTTP、不需要鉴权,
客户端把服务器当子进程拉起来,喂 stdin、读 stdout 即可。

### 2.2 注册到 opencode(推荐)

在 `opencode.json` 里加:

```jsonc
{
  "mcp": {
    "cangjie-kb": {
      "type": "stdio",
      "command": "python",
      "args": [
        "D:/x2cangjie/cangjie-knowledge-mcp/src/cjkb/mcp_server.py",
        "--data-dir", "D:/x2cangjie/cangjie-knowledge-mcp/data"
      ]
    }
  }
}
```

注册后 agent 就能直接调用 9 个工具。可以在对话中直接问:
"用 search_api 查一下 Cangjie 里 HashMap 怎么插入键值对"。

### 2.3 注册到 Claude Desktop

`claude_desktop_config.json`:

```jsonc
{
  "mcpServers": {
    "cangjie-kb": {
      "command": "python",
      "args": [
        "D:/x2cangjie/cangjie-knowledge-mcp/src/cjkb/mcp_server.py",
        "--data-dir", "D:/x2cangjie/cangjie-knowledge-mcp/data"
      ]
    }
  }
}
```

### 2.4 手动测试(不经过 agent)

```bash
# 方式一:命令行演示(走的是同一套 Searcher)
python scripts/query_demo.py "HashMap put key value"
python scripts/query_demo.py --class-members ArrayList
python scripts/query_demo.py --java "java.util.List"

# 方式二:MCP 端到端测试(模拟真实客户端对话)
python tests/mcp_e2e.py
```

---

## 3. 九个 MCP 工具详解

| 工具 | 用途 | 必填参数 | 可选参数 | 返回 |
|---|---|---|---|---|
| `search_api` | API 相似度检索(模糊) | `query` | `module`, `top_k` | 命中的 API 列表(名称/签名/模块/描述/示例) |
| `get_api_details` | 按名字精确查 API | `name` | `module` | 完整记录(签名/参数/返回值/异常/来源) |
| `get_class_members` | 查类的全部成员 | `class_name` | `module` | init/prop/func 成员列表 |
| `find_examples` | 检索示例代码 | `query` | `module`, `top_k` | 示例片段(title/code/来源) |
| `java_to_cangjie` | Java 符号 → Cangjie 等价物 | `java_symbol` | — | j2cjlib 映射 + 术语映射 |
| `error_fix_hint` | 编译错误 → 相关 API + 示例 | `error_text` | `top_k` | 相关 API 和示例 |
| `list_modules` | 列出知识库全部模块 | — | — | 模块名 + API/示例数量 |
| `resolve_java_code` | **渐进式披露分层检索**:Java 代码 → 三级 NL 描述分别检索 | `java_code` | `module`, `top_k` | 三级检索结果 + `best_level` + 最佳命中 |
| `describe_java_code` | 只生成 Java 代码的中英双语 NL 描述(不检索) | `java_code` | — | 三级粒度(api/statement/function)的 NL 描述 |

### 分层检索(`resolve_java_code`)怎么工作

**两阶段检索 = 类型锁定(Stage 1)+ 分层 NL 检索(Stage 2)**。渐进式披露思路:
Java 代码按粒度拆三层,每层生成 NL 描述去仓颉文档检索;但先做类型锁定,
把检索范围从全库缩小到正确的类,再用 NL 找方法——这样"方法名不同但功能相同"
的匹配(put→replace)既能找到,又不会找错类。

**Stage 1 类型锁定**(把 3537 条候选缩小到几个类):

```
输入: HashMap<String,Integer> map = new HashMap<>(); map.put(k, v);
  → java_types 提取器抓出: HashMap, String, Integer
  → java_to_cangjie 查映射表: 3257 条(含 x2cangjie 类型翻译产物)
        HashMap  → HashMap (std.collection)
        BufferedReader → StringReader (std.io)   ← 类型翻译产物的功劳
  → 没有映射的再 search_api 相似度检索
  → 输出 type_candidates: [HashMap@std.collection, ...]
```

**Stage 2 分层 NL 检索 + 交叉验证**:

```
Level 1  api      单个 API 调用    "map.put(k, v)"
        → NL "insert a key-value pair into a map" → 检索
Level 2  statement 语句/代码段     "while ((len = in.read(buf)) > 0) {...}"
        → NL "copy stream data chunk by chunk until EOF" → 检索
Level 3  function 整段函数         "public void copyFile(...) {...}"
        → NL "copy a file" → 检索
```

- 每层独立检索,按"查询词与顶部结果的 token 重叠度"打分
- **交叉验证**:命中结果如果属于 Stage 1 锁定的类型(`parent` 匹配),额外加分
  (`type_matched` 字段)——防止把 `PrettyPrinter.put` 当成 `HashMap.put`
- 分数最高的层为 `best_level`;细粒度没命中就退到粗粒度(渐进披露)
- 最后从锁定类型里取成员 + 示例,打包成 `suggested` 直接可用

### 典型使用场景

**场景 A:翻译一个 Java 片段之前(以 `map.put(key, value)` 为例)**

现在**一步就够**:`resolve_java_code` 内部已经完成了类型锁定 + 方法匹配 +
示例收集,不再需要手动拼 4 个工具。

```
resolve_java_code("HashMap<String,Integer> map = new HashMap<>(); map.put(k, v);")
```

**返回结构**(关键字段):

```jsonc
{
  "java_code": "HashMap<String,Integer> map = new HashMap<>(); map.put(k, v);",
  "java_types": ["HashMap<String,Integer>", "HashMap", "String", "Integer"],  // 提取的 Java 类型
  "type_candidates": [          // Stage 1:锁定的 Cangjie 类型候选
    {"cangjie_type": "HashMap", "module": "std.collection", "confidence": "class_search"},
    ...
  ],
  "best_level": "statement",    // Stage 2:命中最好的粒度层
  "levels": {
    "api":      {"query": "...", "score": 0.2, "type_matched": 0, "apis": [...], "examples": [...]},
    "statement":{"query": "...", "score": 0.5, "type_matched": 1, "apis": [...], "examples": [...]},
    "function": {"query": "...", "score": 0.4, "type_matched": 1, "apis": [...], "examples": [...]}
  },
  "suggested": {                // ★ 可直接使用的建议(两阶段合并的结果)
    "cangjie_type": "HashMap",  //   Cangjie 类型
    "module": "std.collection", //   在哪个模块
    "confidence": "class_search",
    "members": [                //   该类型的方法(含 Java put 的对应)
      {"name": "add(K, V)", "signature": "public func add(key: K, value: V): ?V"},
      {"name": "replace(K, V)", "signature": "public func replace(key: K, value: V): ?V"},
      ...                       //   Cangjie 没有 put!用 add / replace
    ],
    "examples": [...]           //   官方示例
  }
}
```

**怎么读**:
- **`suggested` 是最重要的字段**——类型、模块、方法、示例一次打包。直接用它
  拼翻译 prompt:`HashMap` 在 `std.collection`,插入用 `add(K,V)` 而非 `put`。
- **`best_level`** 告诉你哪个粒度层命中最好(statement/function 通常比 api
  更可靠,因为 NL 描述的是功能意图)
- **`type_candidates`** 是 Stage 1 锁定的类型;`confidence` 为 `mapping` 表示
  来自类型翻译产物(查表命中,最可信),`class_search` 表示相似度检索
- 没配 LLM 时走启发式 NL,质量较低,但**类型锁定不受影响**——`suggested`
  依然能给出正确的类型和成员(类型锁定靠映射表,不靠 NL)

> ⚠️ 关键认知:分层检索解决"方法名不同但功能相同"(put→replace),
> 类型锁定保证方法匹配发生在正确的类里。两者合起来就是完整的
> "Java 方法 → Cangjie 方法"解析链——**一次调用**拿到全部信息。

**如果还想手动确认**(可选):

```
# 类型锁定内部等效操作
java_to_cangjie("java.util.HashMap")   # → 有类型翻译产物后:HashMap (std.collection)
search_api("HashMap")                  # → 相似度检索兜底

# 方法确认(内部已做)
get_class_members("HashMap")           # → 27 个成员,add/replace/get/...
find_examples("HashMap")               # → 官方示例
```

**场景 B:翻译失败后的错误修复**

```
error_fix_hint("cannot find symbol println")
#    → 返回 println 在 std.core 的签名 + 相关示例
```

---

## 4. 每个文件的作用(不含 data/,data 见第 5 节)

```
cangjie-knowledge-mcp/
├── README.md                        # 项目总览(快速上手)
├── requirements.txt                 # 依赖:仅 PyYAML(索引核心零依赖)
├── pyproject.toml                   # 打包配置(pip install -e . 用)
├── config.yaml                      # 语料路径 / 输出目录 / BM25 权重 / LLM 配置
├── .gitignore                       # data/ 与 __pycache__ 不入库
│
├── src/cjkb/                        # 主包
│   ├── __init__.py                  # 版本号
│   ├── models.py                    # 数据模型:ApiRecord / ExampleRecord /
│   │                                #   JavaMapping / KnowledgeBase(内存容器+JSONL读写)
│   ├── config.py                    # 读 config.yaml;相对路径解析到项目根;
│   │                                #   环境变量覆盖(OPENAI_API_KEY 等)
│   ├── nl_generator.py              # ★ Java 代码 → 中英双语 NL 描述
│   │                                #   (api/statement/function 三级粒度;
│   │                                #    LLM 生成,无 key 时启发式兜底)
│   ├── layered_search.py            # ★ 渐进式披露分层检索:
│   │                                #   三级 NL 分别检索 + best_level 判定
│   ├── mcp_server.py                # ★ MCP 服务器:JSON-RPC 协议 + 9 个工具实现 +
│   │                                #   stdio 主循环;零第三方依赖
│   │
│   ├── collector/                   # ── 构建阶段:语料 → 记录 ──
│   │   ├── __init__.py
│   │   ├── corpus_parser.py         # ★ 解析 CangjieCorpus 的 markdown:
│   │   │                            #   *_package_api/*.md → ApiRecord(3543 条)
│   │   │                            #   *_package_samples/*.md → ExampleRecord
│   │   │                            #   manual/extra/ → 语言参考示例
│   │   ├── j2cj_parser.py           # ★ 解析 j2cjlib .cj shim → JavaMapping;
│   │   │                            #   读 java_cangjie_terms.yaml → 术语映射
│   │   └── example_writer.py        # (可选)LLM 补写缺失示例,标记 generated=true;
│   │                                #   增量:跳过已生成的 title
│   │
│   └── index/                       # ── 索引与检索 ──
│       ├── __init__.py
│       ├── bm25.py                  # ★ 纯标准库 BM25:分词(驼峰/下划线/中英混)、
│       │                            #   字段加权、打分、pickle 持久化
│       └── searcher.py              # ★ 高层检索门面:
│                                    #   相似度搜索 / 精确名索引 / Java 术语扩展 /
│                                    #   类成员查询 / 保存加载
│
├── scripts/                         # 命令行入口
│   ├── build_kb.py                  # ★ 构建知识库(collect → index → save)
│   ├── import_type_mappings.py      # ★ 导入 x2cangjie 类型翻译产物 → java_mappings
│   │                                #   (93 → 3257 条,类型锁定从猜变查表)
│   └── query_demo.py                # 命令行检索演示(不经过 MCP)
│
└── tests/
    ├── test_kb.py                   # 单元测试(分词/BM25/解析器/类型提取/检索/MCP)
    ├── mcp_e2e.py                   # MCP 端到端测试(模拟客户端完整对话)
    ├── mcp_layered_e2e.py           # 分层检索端到端测试(启发式,无需 LLM)
    ├── mcp_layered_llm.py           # 分层检索端到端测试(真实 LLM,需配 key)
    └── test_deepseek_api.py         # 直连 DeepSeek API 连通性测试
```

各模块调用关系:

```text
build_kb.py
  ├─► collector/corpus_parser.collect_corpus()   → KnowledgeBase(apis/examples)
  ├─► collector/j2cj_parser.collect_j2c()        → mappings
  ├─► (可选) collector/example_writer.write_examples()  → 补生成示例
  └─► index/searcher.Searcher.build().save()     → data/*.jsonl + *.pkl

import_type_mappings.py
  └─► 读 x2cangjie data/java/type_resolution/*.json
      → KnowledgeBase.mappings 追加 3164 条 → Searcher.build().save()

mcp_server.py (tools/call: resolve_java_code)
  └─► java_types.extract_types()                 → 提取 Java 类型
  └─► layered_search.layered_search()            → 两阶段检索
       ├─ Stage 1: 类型锁定 (java_to_cangjie + search_api)
       ├─ Stage 2: 分层 NL 检索 + 交叉验证 (type_matched)
       └─ 输出 suggested {cangjie_type, module, members, examples}
```

---

## 5. data/ 目录:每个文件是什么

`data/` 是 `build_kb.py` 的**产物**(已被 .gitignore 排除,不提交进 git)。

| 文件 | 大小(约) | 内容 | 谁写 | 谁读 |
|---|---|---|---|---|
| `apis.jsonl` | 5.1 MB | **3537 条 API 记录**,每行一个 JSON:`name / kind / module / library / signature / description / params / returns / exceptions / parent / source / examples / tags` | build_kb | Searcher.load → get_api_details / get_class_members / search_api |
| `examples.jsonl` | 269 KB | **228 条示例**,每行一个 JSON:`title / code / module / library / source / description / tags / generated`。其中 **4 条 `generated=true`**(LLM 补写的) | build_kb (+example_writer) | Searcher.load → find_examples |
| `java_mappings.jsonl` | ~600 KB | **3257 条 Java→Cangjie 映射**,每行:`java_symbol / cangjie_symbol / source / notes / library`。其中 93 条来自 j2cjlib+术语表,3164 条来自 x2cangjie 类型翻译产物(`library=type_resolution`) | build_kb(j2cj_parser) + import_type_mappings | Searcher.load → java_to_cangjie + 类型锁定 + 查询扩展 |
| `modules.json` | 5 KB | **48 个模块的统计**:每个模块的 `library / module_dir / apis / examples` 数量 | build_kb | list_modules 工具 |
| `bm25_apis.pkl` | 1.3 MB | API 记录的 **BM25 索引**(pickle):词频、文档长、IDF 等 | build_kb(index/bm25) | search_api 打分 |
| `bm25_examples.pkl` | 147 KB | 示例的 **BM25 索引**(pickle) | build_kb | find_examples 打分 |
| `.gitkeep` | 0 | 占位,保证空目录进 git | — | — |

### 一行记录长什么样

`apis.jsonl`(截断):

```json
{"name": "ArgumentMode", "kind": "enum", "module": "std.argopt",
 "library": "std", "signature": "public enum ArgumentMode <: ToString & Equatable<ArgumentMode> {",
 "description": "描述选项的参数模式。...", "source": "..."}
```

`examples.jsonl` 中一条 LLM 生成的(注意 `"generated": true`):

```json
{"title": "std.collection_concurrent.xxx (generated)", "code": "import ...",
 "module": "std.collection_concurrent", "library": "std",
 "source": "llm-generated", "generated": true}
```

### data/ 的生命周期

```text
第一次:  build_kb.py --corpus ... --data-dir data   → 生成全部文件
增量:    build_kb.py --write-examples               → 只新增 LLM 示例(自动跳过已生成的)
换语料:  改 config.yaml 或 --corpus 重跑,会整体覆盖
部署:    只需要 data/ 目录 + mcp_server.py,语料目录可以删
```

> 注意:`data/` 里没有原始语料。原始 markdown 在 `misc/CangjieCorpus`,
> `data/` 是解析后的**结构化产物**。删掉 `data/` 重跑 build_kb.py 就能重建。

---

## 6. 端到端示例:从零到能用

```bash
# ① 装依赖(仅 PyYAML)
pip install -r requirements.txt

# ② 构建知识库(读 config.yaml 里的语料路径)
python scripts/build_kb.py

# ③ 验证检索
python scripts/query_demo.py "HashMap put key value"

# ④ 启动 MCP 服务器(前台,stdin/stdout)
python -m cjkb.mcp_server --data-dir data
#    或在 opencode.json / Claude Desktop 里注册(见第 2 节)

# ⑤ 在 agent 对话中使用
#    "search_api: HashMap put key value"
#    "get_class_members: ArrayList"
#    "error_fix_hint: <粘贴编译错误>"
```

---

## 7. 常见问题

**Q: MCP 服务器启动报 "knowledge base not found"?**
A: 先跑 `python scripts/build_kb.py`,确认 `data/apis.jsonl` 存在;
   或 `--data-dir` 指向错了目录。

**Q: 为什么我的 Java 查询命中率不高?**
A: `search_api` 会做 Java 术语扩展(Thread → 线程),但同义改写能力有限。
   建议先 `java_to_cangjie` 拿到 Cangjie 侧名字,再用那个名字查。

**Q: `generated=true` 的示例可靠吗?**
A: 是 LLM 生成的,**未经过 cjpm 编译验证**,仅供参考;官方示例(`generated` 缺失或 false)
   优先级更高。

**Q: 换一台机器怎么迁移?**
A: 拷贝整个项目 + `data/` 即可;语料目录(`misc/CangjieCorpus`)只在重建知识库时需要。
