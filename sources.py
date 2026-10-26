"""Provider adapters. Only this module knows vendor URLs, fields and pagination."""
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request


def api_base(config):
    provider = config.get("provider", "github")
    default = "https://api.github.com" if provider == "github" else ""
    base = config.get("api_base_url", default).rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or
            parsed.query or parsed.fragment):
        raise ValueError("api_base_url 必须是不含凭据、查询或片段的 HTTPS API 根地址")
    return base


def source_identity(config):
    return json.dumps([config.get("provider", "github"), api_base(config), str(config["repo"])])


def token(config=None):
    config = config or {}
    auth = config.get("auth", {})
    provider = config.get("provider", "github")
    host = urllib.parse.urlsplit(api_base(config)).hostname
    name = auth.get("token_env")
    if name:
        if os.environ.get(name):
            return os.environ[name].strip()
    elif provider == "github" and host == "api.github.com":
        for key in ("GH_TOKEN", "GITHUB_TOKEN"):
            if os.environ.get(key):
                return os.environ[key].strip()
    elif provider == "gitlab" and os.environ.get("GITLAB_TOKEN"):
        return os.environ["GITLAB_TOKEN"].strip()
    if auth.get("token_file"):
        secret = Path(auth["token_file"]).read_text(encoding="utf-8").strip()
        if not secret:
            raise ValueError("token_file 为空")
        return secret
    if auth.get("use_gh", provider == "github"):
        gh = shutil.which("gh")
        if gh:
            gh_host = "github.com" if host == "api.github.com" else host
            result = subprocess.run([gh, "auth", "token", "--hostname", gh_host],
                                    capture_output=True, text=True, timeout=20)
            if result.returncode == 0:
                return result.stdout.strip()
    return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("API 返回重定向，请直接配置最终 API 地址（不向重定向目标转发凭据）")


class Transport:
    def __init__(self, config, suffix):
        self.config = config
        self.base = api_base(config) + suffix
        self.token = token(config)
        if self.token and (not self.token.isascii() or any(c.isspace() or ord(c) < 32 for c in self.token)):
            raise ValueError("认证 token 格式无效；请检查凭据来源（不会输出其内容）")
        network = config.get("network", {})
        self.timeout = float(network.get("timeout_seconds", 45))
        if self.timeout <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        context = ssl.create_default_context()
        if network.get("ca_file"):
            context.load_verify_locations(cafile=network["ca_file"])
        proxy = network.get("proxy")
        handlers = [urllib.request.HTTPSHandler(context=context), NoRedirect()]
        if proxy is not None:
            handlers.append(urllib.request.ProxyHandler({"https": proxy} if proxy else {}))
        self.opener = urllib.request.build_opener(*handlers)

    def get(self, path, params=None):
        if not path.startswith("/") and path:
            raise ValueError("API path 必须是相对端点")
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json", "User-Agent": "LML-monitor/2"}
        if self.config.get("provider", "github") == "github":
            headers["Accept"] = "application/vnd.github+json"
        if self.token:
            # Both provider adapters use personal/project access tokens, not session cookies.
            name = "PRIVATE-TOKEN" if self.config.get("provider") == "gitlab" else "Authorization"
            headers[name] = self.token if name == "PRIVATE-TOKEN" else "Bearer " + self.token
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers), timeout=self.timeout) as response:
                return json.load(response), response.headers
        except urllib.error.HTTPError as e:
            retry = e.headers.get("Retry-After") or e.headers.get("X-RateLimit-Reset")
            raise RuntimeError(f"API HTTP {e.code}: {path}; retry/reset={retry}. 请检查地址和仓库读取权限。") from None
        except urllib.error.URLError:
            raise RuntimeError("API 网络或 TLS 连接失败，请检查内网连接、代理和 CA 配置") from None

    def pages(self, path, params=None, key=None):
        page = 1
        while True:
            data, headers = self.get(path, {**(params or {}), "per_page": 100, "page": page})
            yield data if key is None else data[key]
            next_page = headers.get("X-Next-Page")
            if next_page:
                new_page = int(next_page)
                if new_page <= page:
                    raise RuntimeError("API 分页未前进")
                page = new_page
            elif 'rel="next"' in headers.get("Link", ""):
                page += 1
            else:
                break


class GitHub(Transport):
    """Kept importable for the existing GitHub test fixtures."""
    def __init__(self, repo, config=None):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError("GitHub repo 必须是 owner/name")
        super().__init__({**(config or {}), "repo": repo, "provider": "github"}, "/repos/" + repo)


class GitHubSource:
    def __init__(self, client):
        self.client = client

    def issues(self, since):
        params = {"state": "all", "sort": "updated", "direction": "asc"}
        if since:
            params["since"] = since
        for page in self.client.pages("/issues", params):
            yield from (issue for issue in page if "pull_request" not in issue)

    def comments(self, since):
        params = {"sort": "updated", "direction": "asc"}
        if since:
            params["since"] = since
        for page in self.client.pages("/issues/comments", params):
            yield from page

    def default_branch(self):
        return self.client.get("")[0]["default_branch"]

    def head(self, branch):
        return self.client.get("/commits/" + urllib.parse.quote(branch, safe=""))[0]

    def compare(self, before, after):
        path = "/compare/" + before + "..." + after
        result, headers = self.client.get(path, {"per_page": 100, "page": 1})
        result["commits"] = list(result["commits"])
        page = 1
        while 'rel="next"' in headers.get("Link", ""):
            page += 1
            extra, headers = self.client.get(path, {"per_page": 100, "page": page})
            result["commits"].extend(extra["commits"])
        return result

    def files(self, sha):
        files = [file for page in self.client.pages("/commits/" + sha, key="files") for file in page]
        if len(files) >= 3000:
            raise RuntimeError(f"提交 {sha} 的文件列表达到 GitHub 上限，保留游标待人工检查")
        return files


class GitLabSource:
    def __init__(self, config, client=None):
        self.config = config
        self.client = client or Transport(config, "/projects/" + urllib.parse.quote(str(config["repo"]), safe=""))
        self._issues = None
        self._project = None

    def project(self):
        if self._project is None:
            self._project = self.client.get("")[0]
        return self._project

    def issues(self, since):
        # Scan all issue identities, including closed ones: notes may change independently.
        self._issues = [issue for page in self.client.pages("/issues", {"scope": "all", "state": "all"})
                        for issue in page]
        for issue in self._issues:
            yield {"number": issue["iid"], "title": issue["title"], "body": issue.get("description"),
                   "state": "open" if issue["state"] == "opened" else issue["state"],
                   "labels": [{"name": name} for name in issue.get("labels", [])],
                   "assignees": [{"login": user["username"]} for user in issue.get("assignees", [])],
                   "milestone": {"number": issue["milestone"]["iid"]} if issue.get("milestone") else None,
                   "html_url": issue["web_url"]}

    def comments(self, since):
        from datetime import datetime
        cutoff = datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None
        if self._issues is None:
            list(self.issues(since))
        for issue in self._issues:
            for page in self.client.pages(f'/issues/{issue["iid"]}/notes',
                                          {"sort": "desc", "order_by": "updated_at"}):
                stop = False
                for note in page:
                    updated = note.get("updated_at") or note["created_at"]
                    if cutoff and datetime.fromisoformat(updated.replace("Z", "+00:00")) < cutoff:
                        stop = True
                        break
                    if note.get("system"):
                        continue
                    yield {"id": note["id"], "issue_url": f'/issues/{issue["iid"]}',
                           "body": note.get("body"), "updated_at": updated,
                           "user": {"login": note["author"]["username"]},
                           "html_url": issue["web_url"] + "#note_" + str(note["id"])}
                if stop:
                    break

    def default_branch(self):
        return self.project()["default_branch"]

    def normalize_commit(self, commit):
        message = commit.get("message", commit.get("title", ""))
        return {"sha": commit["id"], "commit": {"message": message},
                "html_url": commit.get("web_url") or self.project()["web_url"] + "/-/commit/" + commit["id"]}

    def head(self, branch):
        return self.normalize_commit(self.client.get("/repository/commits/" + urllib.parse.quote(branch, safe=""))[0])

    def compare(self, before, after):
        result = self.client.get("/repository/compare", {"from": before, "to": after, "straight": "true"})[0]
        reverse = self.client.get("/repository/compare", {"from": after, "to": before, "straight": "true"})[0]
        # Fail rather than assume older on-prem versions return a complete timed-out comparison.
        if result.get("compare_timeout") or reverse.get("compare_timeout"):
            raise RuntimeError("GitLab compare 超时或达到限制，保留游标待重试")
        status = "diverged" if reverse["commits"] and result["commits"] else (
            "behind" if reverse["commits"] else "ahead")
        return {"status": status, "commits": [self.normalize_commit(c) for c in result["commits"]],
                "html_url": result.get("web_url") or self.project()["web_url"] + "/-/compare/" + before + "..." + after}

    def files(self, sha):
        # GitLab can silently truncate at the administrator's diff_max_files. Require it explicitly.
        limit = self.config.get("gitlab_diff_max_files")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("GitLab 路径过滤需配置 gitlab_diff_max_files 为管理员确认的实例文件差异上限")
        diffs = [diff for page in self.client.pages(f"/repository/commits/{sha}/diff") for diff in page]
        if len(diffs) >= limit:
            raise RuntimeError("GitLab diff 文件数达到配置上限，无法可靠过滤；保留游标待人工检查")
        files = []
        for diff in diffs:
            status = "renamed" if diff.get("renamed_file") else "removed" if diff.get("deleted_file") else (
                "added" if diff.get("new_file") else "modified")
            file = {"filename": diff.get("new_path") or diff["old_path"], "status": status}
            if diff.get("renamed_file"):
                file["previous_filename"] = diff["old_path"]
            files.append(file)
        return files


def create_source(config):
    provider = config.get("provider", "github")
    if provider == "github":
        return GitHubSource(GitHub(config["repo"], config))
    if provider == "gitlab":
        return GitLabSource(config)
    raise ValueError("provider 目前支持 github 或 gitlab")
