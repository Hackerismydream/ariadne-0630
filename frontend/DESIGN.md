# Ariadne 前端设计系统 — Terminal CLI

> 本文件是 Ariadne Web 前端（Next.js, deep-010）的**设计契约**。codex 生成任何页面/组件前必须先读，所有视觉决策以此为准，不即兴发挥、不产出通用 boilerplate UI。
> 风格出处：terminal / phosphor-monitor 美学，**特化为 Ariadne 的编排语义**——不是通用 terminal 主题，而是「给命令行 agent 世界做的调度台」。

## 0. 为什么是这个风格（设计意图）

Ariadne 的本质是编排命令行 coding agent。界面就该是它调度的那个世界的延伸：一个 phosphor 绿的终端调度台，像 `tmux` 里盯着多个 agent 跑。这不是装饰，是语义自洽——用户在 CLI 世界工作，界面说同一种语言。

**vibe**：Cyber-Industrial / Hacker / System-Level。是干净可用的 ZSH/BASH shell 环境，**不是** Matrix 数字雨（太 cliché）。

## 1. 设计令牌（Design Tokens）

### 颜色（仅暗色，phosphor 显示器调色板）
高对比不可妥协。用 CSS 变量集中定义，禁止组件内写死颜色。

```css
:root {
  --bg:        #0a0a0a;   /* 深黑，非纯 OLED 黑，为扫描线留空间 */
  --fg:        #33ff00;   /* 主色：经典终端绿 */
  --primary:   #33ff00;   /* 亮霓虹绿，主文本/激活态 */
  --secondary: #ffb000;   /* 琥珀色，警告/次要强调 */
  --muted:     #1f521f;   /* 暗绿，边框/非激活文本 */
  --accent:    #33ff00;   /* 同主色，光标/激活 */
  --error:     #ff3333;   /* 亮红 */
  --border:    #1f521f;   /* 暗绿边框 */
}
```

### 字体
- 栈：`'JetBrains Mono', 'Fira Code', 'VT323', monospace`。
- **每一个字符都是等宽**——从最大标题到最小页脚链接，无例外。
- 标题 **ALL CAPS**；body/code 可小写，但同一区块内保持一致。
- 严格模块化字号，headers snap 到网格尺寸（如 `--fs-h1: 2rem; --fs-body: 0.875rem; --fs-mono: 0.8125rem`），不做平滑缩放。

### 圆角 & 边框
- **radius: 0px。绝对无圆角。**
- 边框 `1px solid` 或 `dashed`，用来定义「窗口/窗格」。用暗绿 `--border`。

### 阴影 & 效果
- **无 drop shadow。**
- 主文本用 phosphor 辉光：`text-shadow: 0 0 5px rgba(51, 255, 0, 0.5)`。
- **CRT 扫描线覆盖层**：`pointer-events: none` 的全局 overlay，极淡（opacity ~0.03-0.05）的横向扫描线，给深度但不毁可读性。

### 间距
- 字符网格对齐。间距用 `ch`/`rem` 的整数倍，内容 snap 到刚性网格。

## 2. Ariadne 专属组件（把编排语义缝进 terminal 美学）

这是本设计系统的核心——不是通用 terminal 组件，是 Ariadne 的领域对象在 terminal 里的表达。

### 2.1 状态码徽章（精确映射真实枚举，不编）
用 shell 状态码风格显示状态，颜色按语义：

**TaskStatus**（taskrun 生命周期）：
| 真实值 | 显示 | 颜色 |
|--------|------|------|
| `queued` | `[QUEUED]` | muted 暗绿 |
| `preparing` | `[PREP]` | secondary 琥珀 |
| `claimed` | `[CLAIMED]` | secondary 琥珀 |
| `running` | `[RUNNING]` + 闪烁 `_` | primary 亮绿 |
| `completed` | `[OK]` | primary 亮绿 |
| `failed` | `[ERR]` | error 红 |
| `cancelled` | `[CANCELLED]` | muted 暗绿 |

**IssueStatus**：`backlog/todo/in_progress/done/cancelled` → `[BACKLOG]/[TODO]/[IN-PROG]/[DONE]/[CANCEL]`，同色系规则。

**LeaderDecisionOutcome**：`action/no_action/failed/done` → `> ACTION` / `> NO-OP` / `> FAILED` / `> DONE`（委派决策用 `>` 前缀，像 shell 输出）。

### 2.2 ASCII 进度条（不用饼图/圆环）
执行进度、并发占用一律用 ASCII bar：
```
[||||||||||........] 60%   RUNNING 3/5 taskruns
```
- 填充 `|` 用 primary，空 `.` 用 muted。绝不用 SVG 图表库。

### 2.3 Issue 卡 = 终端窗格（Window/Pane）
```
+--- ISSUE: 写个 hello 函数 ------------------[OK]--+
| agent    : Run Agent 1                            |
| taskrun  : taskrun-7d24b526 [OK] 0.03s            |
| changed  : hello.py (+12 -0)                      |
+---------------------------------------------------+
```
- 黑底 1px 绿边。标题栏用 `+--- TITLE ---+` 或实心反色 bar。
- 右上角嵌状态码。内容等宽文本，字段名左对齐补空格到网格。

### 2.4 实时执行 transcript（详情页核心）
- SSE 事件流式追加，**打字机效果**逐字符出现（呼应 `typing-demo`）。
- 每条事件按 deep-006 的结构化字段渲染：
  - `thinking` → 暗绿斜体，前缀 `~ `
  - `tool_use` → 亮绿，前缀 `$ <tool_name>`（像执行命令）
  - `tool_result` → 缩进，muted
  - 最终结果 → primary，前缀 `> `
- 底部永远一个闪烁块光标 `█`，表示「还在跑」。

### 2.5 New Task 输入 = shell 提示符
```
ariadne@run:~$ ▮
```
- 无 box、无 focus ring。就是提示符 + 输入区 + 闪烁块光标 `█`。
- backend/mode 选择用 shell flag 风格：`--backend codex --squad`。

### 2.6 按钮
- 结构：文字包在括号里 `[ RUN ]` `[ CANCEL ]`，或实心反色块。
- hover：背景填充 primary，文字变黑（反色 inverted video）。
- active：文字下移 1px 或快速闪烁。

## 3. 布局策略

像 `tmux`/`vim` split 的终端窗口网格：
- **列表页**：issue 列表如终端 `ls -l` 输出，每行一个 issue + 状态码 + agent + 活跃 taskrun 数。顶部有 `> N agents working` 实时指示 + `[ NEW TASK ]`。
- **详情页**：上=issue 元信息窗格，中=实时 transcript 窗格，下=diff 窗格。窗格间用 ASCII 分隔线 `================` / `----------------` / `//`。
- 严格字符网格对齐。

## 4. Non-Genericness（大胆因子，必须有）

不做出来就是失败——这些是让它「不像 boilerplate」的关键：
- **ASCII art logo**：Ariadne 的 logo 用 ASCII art（如 `ARIADNE` 的 block 字体）。
- **打字机效果**：hero 文本、transcript 逐字符出现。
- **原始数据可视化**：所有 stats 用 ASCII bar `[||||....]`，绝不用图表库。
- **闪烁光标**：`█` 或 `_` 是界面的心跳，running 态、输入态都有。

## 5. 动效

- `animate-blink`：光标标准闪烁（1s 步进）。
- `glitch`：hover 时偶发的轻微文字位移（克制，不滥用）。
- `typing`：hero / transcript 打字机动画。
- **扫描线**：静态或极缓慢移动的 CRT 扫描线 overlay。
- 全部动效尊重 `prefers-reduced-motion`（可访问性，见下）。

## 6. 可访问性（不可牺牲）

- 高对比是风格也是 a11y 优势，但**绿/红对色盲不友好**——状态不能只靠颜色，必须同时有文字码（`[OK]`/`[ERR]`）。这是本设计「状态码优先」的 a11y 理由。
- `prefers-reduced-motion` 时关闭打字机/glitch/扫描线动画，保留静态呈现。
- 语义 HTML + ARIA：transcript 用 `role="log" aria-live="polite"`；状态码有 `aria-label` 全称。
- 焦点可见：虽无 focus ring，但键盘焦点用反色块或闪烁光标明确标示，不能「无反馈」。
- 对比度：primary 绿 on 黑 ≈ 15:1，远超 WCAG AAA。琥珀/红同样验证 ≥ 4.5:1。

> 注：完整 WCAG 合规需辅助技术实测 + 专家评审，本文件只保证设计层面的对比/多通道/动效可关闭。

## 7. 给 codex 的落地约束

- 令牌集中在一处（CSS 变量 / Tailwind config），组件引用令牌，**禁止一次性写死颜色/间距**。
- 组件可复用可组合：`StatusBadge`、`AsciiProgress`、`Pane`、`ShellPrompt`、`Transcript` 是原子件，页面由它们组合。
- 匹配 Next.js + TypeScript + Tailwind 既有模式（deep-010 定）。
- 状态码组件的枚举值**从 API 响应取真实值映射**，不在前端硬编码状态列表（真实枚举见 §2.1，来自 `models.py`）。
- 留下比接手时更干净的代码：无一次性样式、无重复令牌。
