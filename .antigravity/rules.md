# Project Analysis — Read Only
 
## Scope
 
These rules apply when analyzing, understanding, documenting, reverse-engineering,
or explaining this project.
 
## Mandatory restrictions
 
- Treat the repository as READ-ONLY.
- Do NOT create any file.
- Do NOT modify any existing file.
- Do NOT delete, rename, move, or refactor files.
- Do NOT install packages or dependencies.
- Do NOT change configuration.
- Do NOT generate README.md, HANDOFF.md, or other documentation files.
- Return all analysis directly in the response.
 
## Source of truth
 
- Use the actual repository source code as the primary source of truth.
- Do not invent functionality, architecture, APIs, classes, functions, or relationships.
- Trace actual imports, calls, references, and execution paths where possible.
- Distinguish verified behavior from inferred behavior.
- If something cannot be verified from the repository, explicitly say so.
 
## Code analysis
 
When analyzing the project:
 
- Identify important files and their responsibilities.
- Identify important classes and their responsibilities.
- Identify important functions/methods.
- Explain who calls important functions.
- Explain what important functions call.
- Explain inputs and outputs.
- Explain how components connect to the overall application flow.
- Follow the actual runtime flow from entry point to final result.
- Identify API, CLI, webhook, worker, database, AI/LLM, MCP, and external integration flows when present.
 
## Source snippets
 
Use SMALL source-code snippets only when they help explain an important implementation detail.
 
Prioritize snippets for:
 
- Application entry points
- API handlers
- Core orchestration
- Important business logic
- Important classes/methods
- Database operations
- LLM/provider calls
- MCP/tool calls
- External integrations
- State transitions
- Important error handling
 
Do NOT dump complete files or large sections of source code.
 
For every important snippet, identify:
 
Source file:
Class/function:
Purpose:
 
## Response behavior
 
- Use normal Markdown chat responses.
- Use headings, tables, lists, and text flow diagrams where useful.
- Keep each analysis request focused on the requested subsystem.
- Do not unnecessarily analyze unrelated files.
- Do not repeat information already established.
- Prefer concise technical explanations over large amounts of prose.
- If the requested analysis is too large for one response, finish at a clean section boundary and state what remains.
 
## Documentation behavior
 
When asked for README or HANDOFF content:
 
- Generate the content directly in the response.
- Do NOT create the actual documentation file.
- Base the documentation on the analysis and repository evidence.
- Do not introduce information that was not verified or clearly identified as inference.
 
## Security
 
- Never expose secrets, API keys, passwords, tokens, credentials, or private keys.
- If configuration contains secrets, show only the variable/key name and redact the value.
 
## Priority
 
Accuracy of the repository analysis is more important than completeness of every file.
 
Prioritize:
 
1. Runtime execution path
2. Core business/application logic
3. Important components
4. Component relationships
5. APIs and external entry points
6. AI/LLM/agent flow
7. Database/data flow
8. Configuration/integrations
9. Error handling
10. Testing/deployment