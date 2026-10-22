### Accuracy by configuration and model

| Configuration | sonnet | haiku |
| --- | ---: | ---: |
| LLM alone | 60% (18/30) | 53% (16/30) |
| RAG over runbooks | 63% (19/30) | 53% (16/30) |
| Agent over MCP tools | 100% (30/30) | 93% (28/30) |

### The tier that decides it: `evidence_required`

| Configuration | sonnet | haiku |
| --- | ---: | ---: |
| LLM alone | 9% (1/11) | 9% (1/11) |
| RAG over runbooks | 9% (1/11) | 9% (1/11) |
| Agent over MCP tools | 100% (11/11) | 100% (11/11) |

### Cost and abstention

| Model | Configuration | Cost / incident | Abstained | Confidently wrong |
| --- | --- | ---: | ---: | ---: |
| sonnet | LLM alone | $0.0087 | 40% | 0 |
| sonnet | RAG over runbooks | $0.0130 | 37% | 0 |
| sonnet | Agent over MCP tools | $0.0618 | 0% | 0 |
| haiku | LLM alone | $0.0008 | 47% | 0 |
| haiku | RAG over runbooks | $0.0011 | 47% | 0 |
| haiku | Agent over MCP tools | $0.0047 | 7% | 0 |
