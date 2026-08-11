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

渐进式披露思路:Java 代码按粒度拆三层,每层生成 NL 描述去仓颉文档检索。

```
Level 1  api      单个 API 调用    "map.put(k, v)"
        → NL "insert a key-value pair into a map"
        → 检索(细粒度,可能无 1:1 对应)
Level 2  statement 语句/代码段     "while ((len = in.read(buf)) > 0) {...}"
        → NL "copy stream data chunk by chunk until EOF"
        → 检索(对应一个或几个 Cangjie API)
Level 3  function 整段函数         "public void copyFile(...) {...}"
        → NL "copy a file"
        → 检索(对应整个功能模块)
```

- NL 描述由 LLM 生成(配置了 key 时)或启发式(驼峰拆分 + 术语映射)兜底
- 每层独立检索,按"查询词与顶部结果的 token 重叠度"打分,分最高者为 `best_level`
- **用法**:从 `best_level` 开始取结果;细粒度没命中就退到粗粒度(渐进披露)

### 典型使用场景

**场景 A:翻译一个 Java 片段之前**

```
# 0.(推荐)直接把整个片段丢给分层检索
resolve_java_code("map.put(key, value);")
#    → statement 层命中 replace(K,V)/add(K,V)(Cangjie 中 put 的正确对应)

# 1. 先确认 Java 类型在 Cangjie 里对应什么
java_to_cangjie("java.util.HashMap")
#    → 没有直接 shim,则 search_api("HashMap")

# 2. 查目标类的实际方法名(避免把 Java 的 put 直接搬过来)
get_class_members("HashMap")
#    → add(K,V) / replace(K,V) / get(K) ... (Cangjie 没有 put!)

# 3. 找官方示例做参考
find_examples("HashMap")
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
│   └── query_demo.py                # 命令行检索演示(不经过 MCP)
│
└── tests/
    ├── test_kb.py                   # 单元测试(分词/BM25/解析器/检索/MCP 协议)
    ├── mcp_e2e.py                   # MCP 端到端测试(模拟客户端完整对话)
    └── test_deepseek_api.py         # 直连 DeepSeek API 连通性测试
```

各模块调用关系:

```text
build_kb.py
  ├─► collector/corpus_parser.collect_corpus()   → KnowledgeBase(apis/examples)
  ├─► collector/j2cj_parser.collect_j2c()        → mappings
  ├─► (可选) collector/example_writer.write_examples()  → 补生成示例
  └─► index/searcher.Searcher.build().save()     → data/*.jsonl + *.pkl

mcp_server.py
  └─► index/searcher.Searcher.load(data_dir)     → 只读 data/
       └─► index/bm25.BM25Index.load()           → 反序列化 pkl
       └─► models.KnowledgeBase.from_jsonl()     → 反序列化 JSONL
```

---

## 5. data/ 目录:每个文件是什么

`data/` 是 `build_kb.py` 的**产物**(已被 .gitignore 排除,不提交进 git)。

| 文件 | 大小(约) | 内容 | 谁写 | 谁读 |
|---|---|---|---|---|
| `apis.jsonl` | 5.1 MB | **3537 条 API 记录**,每行一个 JSON:`name / kind / module / library / signature / description / params / returns / exceptions / parent / source / examples / tags` | build_kb | Searcher.load → get_api_details / get_class_members / search_api |
| `examples.jsonl` | 269 KB | **228 条示例**,每行一个 JSON:`title / code / module / library / source / description / tags / generated`。其中 **4 条 `generated=true`**(LLM 补写的) | build_kb (+example_writer) | Searcher.load → find_examples |
| `java_mappings.jsonl` | 15 KB | **93 条 Java→Cangjie 映射**,每行:`java_symbol / cangjie_symbol / source / notes / library` | build_kb(j2cj_parser) | Searcher.load → java_to_cangjie + 查询扩展 |
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
