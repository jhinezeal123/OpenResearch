"""Local JSON boundary to AI Scientist's existing Kaggle bootstrap package.

No Workbench server or MCP transport is involved. Credentials remain in the
donor's account profiles; OpenResearch owns only its ephemeral session state.
"""
import contextlib
import hashlib
import json
from pathlib import Path
import sys


def session_id(run_id):
    import uuid
    return uuid.UUID(run_id).hex


def accounts(account_store):
    result = []
    for key, entry in account_store.load_registry()["accounts"].items():
        try:
            account_store.read_token(key)
            configured = True
        except (OSError, ValueError):
            configured = False
        result.append({"id": key, "alias": entry.get("alias", key),
                       "username": entry.get("username"), "configured": configured})
    return result


def control(session, action, sid, account, state_root):
    receipt = state_root / sid / "stop-confirmed.json"
    if receipt.is_file():
        previous = session.load(receipt)
        if previous.get("session_id") == sid and previous.get("account") == account and previous.get("stopped") is True:
            return previous
    result = session.control(action, sid, account)
    if result.get("stopped") is True:
        result["account"] = account
        session.save(receipt, result)
    return result


def previous_stopped(session, state_root, sid, account):
    # The account lease can survive a child exiting before prepare/submit. The
    # donor always journals submit intent BEFORE making a provider request.
    if not (state_root / sid / "submit-intent.json").exists():
        return True
    return control(session, "status", sid, account, state_root).get("stopped") is True


def dispatch(request):
    runtime = request["runtime"]
    donor = Path(runtime["donorRoot"]).resolve(strict=True)
    if not (donor / "interface_ai_scientist/bootstrap_service.py").is_file():
        raise ValueError("Choose the bundled Kaggle package from AI Scientist")
    sys.path.insert(0, str(donor))
    import account_store
    from interface_ai_scientist import session
    state_root = Path(request["stateRoot"]).resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    session.DEFAULT_STATE = state_root
    action = request["action"]
    if action == "accounts":
        return {"accounts": accounts(account_store), "accelerators": list(session.ACCELERATORS)}

    account, _ = account_store.resolve(request["account"])
    if action == "readiness":
        from account_runtime import account_readiness
        return account_readiness(account)

    sid = session_id(request["runId"])
    if action in {"stop", "status"}:
        return control(session, action, sid, account, state_root)
    if action != "start":
        raise ValueError("Unknown Kaggle bridge operation")

    # Keep the existing donor admission/submit journal. A local account lease
    # also covers the gap before Kaggle's active-session listing sees a submit.
    lease_id = hashlib.sha256(("openresearch:" + account).encode()).hexdigest()[:32]
    lease_file = state_root / ("account-" + lease_id + ".json")
    with session.request_lock(lease_id, namespace="assets"):
        if not (state_root / sid / "state.json").exists():
            if lease_file.exists():
                previous = session.load(lease_file)["sessionId"]
                if previous != sid:
                    if not previous_stopped(session, state_root, previous, account):
                        raise RuntimeError("This account already owns an unfinished OpenResearch notebook")
            from account_runtime import account_readiness
            readiness = account_readiness(account)
            if readiness.get("readiness") != "verified_idle":
                return {"ssh_status": "BLOCKED", "submission_status": "NOT_SUBMITTED",
                        "required_action": "Kaggle account must be signed in and idle before launch",
                        "readiness": readiness.get("readiness")}
            session.save(lease_file, {"sessionId": sid})
        from proxy_control import ensure_proxy
        proxy = ensure_proxy()
        try:
            from interface_ai_scientist.bootstrap_service import start
            result = start(account=account, request_id=sid,
                           ttl_seconds=request["ttlSeconds"], accelerator=request["accelerator"],
                           competition_sources=request.get("competitionSources", []),
                           dataset_sources=request.get("datasetSources", []), wait_seconds=15)
        finally:
            if proxy is not None and proxy.poll() is None:
                proxy.terminate()
                proxy.wait(timeout=5)
    if result.get("ssh_status") == "READY":
        # The shared SSH backend handles transport. Only the user's experiment
        # command needs the notebook environment that Tailcat shells omit.
        result["wrapped_command"] = session.notebook_environment_command(request["command"])
    return result


def main():
    request = json.load(sys.stdin)
    try:
        # Donor stop() prints a receipt; keep stdout one JSON object per call.
        with contextlib.redirect_stdout(sys.stderr):
            result = dispatch(request)
        print(json.dumps({"ok": True, "result": result}), flush=True)
    except Exception as error:
        # Full diagnostic stays in the private local adapter log. UI errors
        # carry the operation and type, never credentials or tunnel endpoints.
        print(f"Kaggle {request.get('action')}: {type(error).__name__}: {error}", file=sys.stderr)
        print(json.dumps({"ok": False, "errorType": type(error).__name__}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
