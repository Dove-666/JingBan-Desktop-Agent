"""Use local Git authentication in memory; never persist or log credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/release-validation/github"
REPOSITORY = "Dove-666/JingBan-Desktop-Agent"
API = "https://api.github.com"


class ProgressReader:
    def __init__(self, stream, name, size):
        self.stream, self.name, self.size = stream, name, size
        self.sent, self.last = 0, time.monotonic()

    def __len__(self):
        return self.size

    def read(self, amount=-1):
        chunk = self.stream.read(amount)
        self.sent += len(chunk)
        if time.monotonic() - self.last >= 15:
            print(json.dumps({"asset": self.name, "bytes_sent": self.sent, "total": self.size}), flush=True)
            self.last = time.monotonic()
        return chunk

    def tell(self):
        return self.stream.tell()

    def fileno(self):
        return self.stream.fileno()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def session():
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    result = subprocess.run(["git", "credential", "fill"], cwd=ROOT,
        input=b"protocol=https\nhost=github.com\n\n", capture_output=True, timeout=30, env=env)
    if result.returncode:
        raise RuntimeError("Local GitHub authentication unavailable")
    fields = dict(line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line)
    token = fields.get("password", "")
    if not token:
        raise RuntimeError("Local GitHub authentication unavailable")
    client = requests.Session()
    client.headers.update({"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Jingban-Release"})
    # Reuse an explicitly configured local Git proxy, if any; never log its value.
    proxy = subprocess.run(["git", "config", "--get", "http.proxy"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if proxy:
        client.proxies.update({"https": proxy})
    fields.clear()
    token = ""
    response = client.get(API + "/user", timeout=30, allow_redirects=False)
    if response.status_code != 200 or response.json().get("login") != "Dove-666":
        raise RuntimeError("GitHub account verification failed")
    return client


def request(client, method, path, **kwargs):
    response = client.request(method, API + path, timeout=45, allow_redirects=False, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError("GitHub request failed with HTTP " + str(response.status_code))
    return response.json() if response.content else None


def verify_remote_assets(actual, expected):
    if len(actual) != len(expected) or {x["name"] for x in actual} != {x["name"] for x in expected}:
        raise RuntimeError("Release asset set changed")
    expected_by_name = {x["name"]: x for x in expected}
    for asset in actual:
        prior = expected_by_name[asset["name"]]
        if (asset.get("state") != "uploaded" or asset["size"] != prior["bytes"]
                or asset.get("digest") != "sha256:" + prior["sha256"]):
            raise RuntimeError("Release asset bytes differ from verification")


def verify_release_target(client, release, record):
    if release["tag_name"] != record["tag"] or release["target_commitish"] != record["commit"]:
        raise RuntimeError("Draft release target changed")
    response = client.get(API + "/repos/" + REPOSITORY + "/git/ref/tags/" + record["tag"],
                          timeout=30, allow_redirects=False)
    if response.status_code == 404 and release["draft"]:
        return  # GitHub may create the new tag only when the draft is published.
    if response.status_code != 200:
        raise RuntimeError("Release tag lookup failed")
    target = response.json()["object"]
    if target["type"] != "commit" or target["sha"] != record["commit"]:
        raise RuntimeError("Release tag target changed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("preflight", "upload", "publish"))
    parser.add_argument("--commit")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    client = session()
    repo = request(client, "GET", "/repos/" + REPOSITORY)
    assert repo["id"] == 1438132326 and repo["default_branch"] == "main", "Repository identity changed; stopped"
    if args.action == "preflight":
        print(json.dumps({"authenticated": True, "repository_private": repo["private"], "account_verified": True}), flush=True)
        return
    record_path = OUT / "github-release.json"
    if args.action == "publish":
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert record["assets_complete"] and record["tag"] == "v1.0.2"
        release = request(client, "GET", "/repos/" + REPOSITORY + "/releases/" + str(record["id"]))
        assert release["draft"] and release["tag_name"] == record["tag"]
        verify_release_target(client, release, record)
        actual = request(client, "GET", "/repos/" + REPOSITORY + "/releases/" + str(record["id"]) + "/assets?per_page=100")
        verify_remote_assets(actual, record["assets"])
        body = "安装后在设置中填写自己的 API。已安装用户请先保存草稿并从托盘退出旧程序。\n\nWindows 安装包：Jingban-Setup.exe；免安装包：Jingban.zip。\nAndroid：Jingban.apk；通用架构：Jingban-Universal.apk。\n\n新配置默认浅色、角色大小 100%、电脑 Agent 功能开启；保留角色卡和对话同步。可使用同页 SHA256SUMS.txt 校验下载文件。"
        release = request(client, "PATCH", "/repos/" + REPOSITORY + "/releases/" + str(record["id"]), json={"draft": False, "body": body})
        verify_release_target(client, release, record)
        record.update(published=True, url=release["html_url"])
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"published": True, "url": release["html_url"]}), flush=True)
        return
    assert args.commit and len(args.commit) == 40
    tag_check = client.get(API + "/repos/" + REPOSITORY + "/git/ref/tags/v1.0.2", timeout=30, allow_redirects=False)
    assert tag_check.status_code in (200, 404), "Tag lookup failed"
    if tag_check.status_code == 200:
        tag_object = tag_check.json()["object"]
        assert tag_object["type"] == "commit" and tag_object["sha"] == args.commit, "Existing tag must not be replaced"
    files = [ROOT / "deliverables/windows/1.0.2/鲸伴安装.exe", ROOT / "deliverables/windows/1.0.2/鲸伴.zip",
        ROOT / "android/deliverables/1.19.1/鲸伴.apk", ROOT / "android/deliverables/1.19.1/鲸伴通用.apk"]
    remote_names = {"鲸伴安装.exe": "Jingban-Setup.exe", "鲸伴.zip": "Jingban.zip",
        "鲸伴.apk": "Jingban.apk", "鲸伴通用.apk": "Jingban-Universal.apk", "SHA256SUMS.txt": "SHA256SUMS.txt"}
    assert all(path.is_file() for path in files)
    assert all(path.stat().st_size < 2 * 1024 ** 3 for path in files)
    ready = json.loads((ROOT / "artifacts/release-validation/release-ready.json").read_text(encoding="utf-8"))
    assert ready["passed"] and ready["desktop"] == "1.0.2" and ready["android"] == "1.19.1"
    approved = {row["path"]: row for row in ready["packages"]}
    for path in files:
        row = approved[path.relative_to(ROOT).as_posix()]
        if sha(path) != row["sha256"] or path.stat().st_size != row["bytes"]:
            raise RuntimeError("Local delivery changed after acceptance")
    sums = OUT / "SHA256SUMS.txt"
    sums.write_text("".join(sha(path) + "  " + remote_names[path.name] + "\n" for path in files), encoding="utf-8", newline="\n")
    files.append(sums)
    if record_path.exists():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert record["commit"] == args.commit and not record.get("published")
        release = request(client, "GET", "/repos/" + REPOSITORY + "/releases/" + str(record["id"]))
        assert release["draft"] and release["tag_name"] == "v1.0.2"
    else:
        releases = request(client, "GET", "/repos/" + REPOSITORY + "/releases?per_page=100")
        assert not any(x["tag_name"] == "v1.0.2" for x in releases), "An existing release must not be replaced"
        release = request(client, "POST", "/repos/" + REPOSITORY + "/releases", json={
            "tag_name": "v1.0.2", "target_commitish": args.commit, "name": "鲸伴", "draft": True,
            "prerelease": False, "body": "安装后在设置中填写自己的 API。已安装用户请先保存草稿并从托盘退出旧程序。\n\nWindows：鲸伴安装.exe；免安装：鲸伴.zip。Android：鲸伴.apk；通用架构：鲸伴通用.apk。\n\n新配置默认浅色、角色大小 100%、电脑 Agent 功能开启；保留角色卡和对话同步。"})
        record = {"id": release["id"], "tag": "v1.0.2", "commit": args.commit, "assets": [], "assets_complete": False, "published": False}
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    upload_url = release["upload_url"].split("{", 1)[0]
    location = urlsplit(upload_url)
    assert location.scheme == "https" and location.hostname == "uploads.github.com" and not location.username
    existing = request(client, "GET", "/repos/" + REPOSITORY + "/releases/" + str(record["id"]) + "/assets?per_page=100")
    for path in files:
        fingerprint, size = sha(path), path.stat().st_size
        remote_name = remote_names[path.name]
        prior = next((x for x in record["assets"] if x["name"] == remote_name), None)
        remote = next((x for x in existing if x["name"] == remote_name), None)
        if prior and remote:
            assert prior["sha256"] == fingerprint
            verify_remote_assets([remote], [prior])
            continue
        if remote:
            pending = record.get("pending", {})
            assert pending.get("name") == remote_name and pending.get("sha256") == fingerprint
            if remote["state"] == "uploaded" and remote["size"] == size and remote.get("digest") == "sha256:" + fingerprint:
                record["assets"].append({"id": remote["id"], "name": remote_name, "local_name": path.name, "bytes": size,
                    "sha256": fingerprint, "server_sha256_verified": True})
                record.pop("pending", None)
                record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                continue
            assert remote["state"] == "starter" and release["draft"], "Existing complete assets must not be replaced"
            request(client, "DELETE", "/repos/" + REPOSITORY + "/releases/assets/" + str(remote["id"]))
        record["pending"] = {"name": remote_name, "sha256": fingerprint, "bytes": size}
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"uploading": path.name, "bytes": size}), flush=True)
        with path.open("rb") as stream:
            response = client.post(upload_url, params={"name": remote_name, "label": path.name}, data=ProgressReader(stream, path.name, size),
                headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
                timeout=(30, 120), allow_redirects=False)
        if response.status_code != 201:
            raise RuntimeError("Asset upload failed with HTTP " + str(response.status_code))
        remote = response.json()
        assert remote["size"] == size and remote["state"] == "uploaded" and remote["name"] == remote_name
        server_digest = remote.get("digest")
        assert server_digest is None or server_digest == "sha256:" + fingerprint
        record["assets"].append({"id": remote["id"], "name": remote_name, "local_name": path.name, "bytes": size, "sha256": fingerprint,
            "server_sha256_verified": server_digest == "sha256:" + fingerprint})
        record.pop("pending", None)
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"uploaded": path.name, "server_digest_verified": bool(server_digest)}), flush=True)
    record["assets_complete"] = len(record["assets"]) == len(files)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"assets_complete": record["assets_complete"], "assets": len(record["assets"])}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never render request objects, headers, credential helper output or response bodies.
        reason = str(error)[:180] if isinstance(error, (AssertionError, RuntimeError)) else "Operation failed; sensitive diagnostics suppressed"
        print(json.dumps({"failed": True, "type": type(error).__name__, "reason": reason}), flush=True)
        raise SystemExit(1)
