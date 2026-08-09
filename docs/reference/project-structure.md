# Project Structure

```
├── infrastructure/           # CDK infrastructure code
│   ├── src/
│   │   ├── stacks/          # CloudFormation stacks
│   │   ├── constructs/      # Reusable CDK constructs
│   │   └── functions/       # AWS Lambda function code
│   ├── scripts/             # Deployment & setup scripts
│   └── test/                # Infrastructure tests
├── backend/
│   ├── voice-agent/         # Voice pipeline container (hub)
│   │   ├── app/
│   │   │   ├── services/    # STT/TTS/LLM service factories
│   │   │   ├── tools/       # Tool framework + built-in tools
│   │   │   ├── a2a/         # A2A capability agent integration
│   │   │   ├── pipeline_ecs.py   # Pipecat pipeline configuration (Daily transport)
│   │   │   ├── pipeline_local.py # Pipecat pipeline configuration (SmallWebRTC transport)
│   │   │   ├── local_main.py     # Local prototyping entry point (FastAPI + browser WebRTC)
│   │   │   ├── observability.py  # Metrics observers
│   │   │   └── service_main.py   # HTTP service (aiohttp)
│   │   ├── tests/           # Python tests
│   │   └── Dockerfile       # Container definition (Python 3.12)
│   └── agents/              # A2A capability agents (spokes)
│       ├── knowledge-base-agent/  # KB RAG agent
│       └── crm-agent/            # CRM agent (5 tools)
├── docs/
│   ├── guides/              # Developer guides
│   ├── patterns/            # Architecture patterns
│   └── reference/           # Reference documentation
└── resources/               # Sample data (KB documents)
```
