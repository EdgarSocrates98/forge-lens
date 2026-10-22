"""Provider fixture do the-forge que falha de verdade no `execute`.

Forge Protocol real: o core invoca ``<argv> <op>`` com um Request JSON em
stdin e espera um Response JSON em stdout. Este fixture responde
``describe``/``health`` honestamente — declarando a capability
``demo.failcrash`` — e quebra com exit 3 no ``execute``, produzindo um
``receipt.status = "provider_failure"`` e um span ``provider:failing-forge``
com ``status = "error"`` na telemetria nativa do run (tudo medido e gravado
pelo próprio theforge; nada é simulado aqui).
"""

import json
import sys

op = sys.argv[1] if len(sys.argv) > 1 else ""
raw = sys.stdin.buffer.read() or b"{}"
try:
    req = json.loads(raw)
except Exception:
    req = {}
rid = str(req.get("request_id", "unknown"))


def respond(status, payload=None, error=None):
    out = {
        "protocol": "forge/v1",
        "kind": "Response",
        "request_id": rid,
        "op": op,
        "producer": {"id": "failing-forge", "version": "0.0.1"},
        "status": status,
        "payload": payload or {},
        "error": error,
        "limitations": [],
        "unknowns": [],
    }
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


if op == "describe":
    respond("ok", payload={
        "schema": "theforge/ForgeManifest/v1",
        "id": "failing-forge",
        "version": "0.0.1",
        "protocols": ["forge/v1"],
        "ops": ["describe", "health", "execute"],
        "domains": ["demo"],
        "capabilities": [{
            "id": "demo.failcrash",
            "actions": ["explode"],
            "default_action": "explode",
            "state": "supported",
            "operation_class": "read_only",
            "description": "Falha deliberada no execute (fixture de falha).",
            "signals": {
                "keywords": ["explode", "failcrash"],
                "file_globs": [],
                "dependencies": [],
            },
        }],
        "execution": {
            "local": True,
            "offline": True,
            "requires_network": False,
            "deterministic": True,
        },
        "limitations": ["fixture provider; execute always crashes"],
        "unknowns": [],
    })
elif op == "health":
    respond("ok", payload={
        "status": "ok",
        "checks": [{"name": "self", "ok": True, "detail": ""}],
    })
elif op == "execute":
    sys.stderr.write("failing-forge: deliberate crash on execute\n")
    sys.stderr.flush()
    sys.exit(3)
else:
    respond("refused", error={
        "code": "FF-OP-UNSUPPORTED",
        "detail": f"op {op} not supported",
        "field": "op",
        "unlock": "ops: describe, health, execute",
    })
