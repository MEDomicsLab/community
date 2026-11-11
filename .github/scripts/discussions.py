import json
import os
import re
import subprocess
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parents[2] / "discussions.json"
GENERAL_MESSAGES = {
    "questions": "Please review this question.",
    "feedback": "Please triage this feedback.",
}
REVIEW_NOTIFICATION_PATTERN = r"Please triage this .+ review\."


def graphql(query, **variables):
    result = subprocess.run(
        ["gh", "api", "graphql", "--input", "-"],
        input=json.dumps({"query": query, "variables": variables}),
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "GitHub GraphQL request failed.")
    return json.loads(result.stdout)["data"]


def paginate(query, connection, **variables):
    cursor = None
    while True:
        page = connection(graphql(query, cursor=cursor, **variables))
        yield from page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return
        cursor = page["pageInfo"]["endCursor"]


def scope_from_body(body):
    _, _, fields = ("\n" + body.replace("\r\n", "\n")).partition("\n### ")
    heading, _, value = fields.partition("\n")
    if heading != "Scope":
        return None
    return value.partition("\n### ")[0].strip()


def already_notified(comments, notification_pattern):
    return any(
        (comment.get("author") or {}).get("login") in {"github-actions", "github-actions[bot]"}
        and re.search(
            rf" {notification_pattern}\Z",
            (comment["body"].splitlines() or [""])[0],
        )
        for comment in comments
    )


def route(config, event):
    discussion = event["discussion"]
    if discussion["number"] in config["excluded_discussions"]:
        return

    category = discussion["category"]["slug"]
    kind = next((key for key in GENERAL_MESSAGES if config[key]["category"] == category), None)
    node_id = discussion["node_id"]
    if kind is not None:
        scope = config["scopes"].get(scope_from_body(discussion["body"] or ""))
        if scope is None:
            return

        owner, repo = event["repository"]["full_name"].split("/")
        label = scope["label"]
        data = graphql(
            """
            query($owner: String!, $repo: String!, $label: String!) {
              repository(owner: $owner, name: $repo) { label(name: $label) { id } }
            }
            """,
            owner=owner,
            repo=repo,
            label=label,
        )
        target = data["repository"]["label"]
        if not target:
            raise ValueError(f"Create the configured scope label '{label}' in {owner}/{repo}.")
        graphql(
            """
            mutation($id: ID!, $labels: [ID!]!) {
              addLabelsToLabelable(input: {labelableId: $id, labelIds: $labels}) {
                labelable { ... on Discussion { id } }
              }
            }
            """,
            id=node_id,
            labels=[target["id"]],
        )
        maintainers = scope["maintainers"]
        message = GENERAL_MESSAGES[kind]
        notification_pattern = re.escape(message)
    else:
        review = next(
            (entry for entry in config["proof_of_concept"].values() if entry["category"] == category),
            None,
        )
        if review is None:
            return
        maintainers = review["maintainers"]
        message = f"Please triage this {review['project']} review."
        notification_pattern = REVIEW_NOTIFICATION_PATTERN

    if not maintainers:
        return
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login) for login in maintainers):
        raise ValueError(f"Invalid GitHub username for category '{category}'.")
    comments = paginate(
        """
        query($id: ID!, $cursor: String) {
          node(id: $id) {
            ... on Discussion {
              comments(first: 100, after: $cursor) {
                nodes { author { login } body }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
        """,
        lambda data: data["node"]["comments"],
        id=node_id,
    )
    if already_notified(comments, notification_pattern):
        return

    mentions = " ".join(f"@{login}" for login in dict.fromkeys(maintainers))
    graphql(
        """
        mutation($id: ID!, $body: String!) {
          addDiscussionComment(input: {discussionId: $id, body: $body}) { comment { url } }
        }
        """,
        id=node_id,
        body=f"{mentions} {message}",
    )


def main():
    config = json.loads(CONFIG_PATH.read_text())
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    route(config, event)


if __name__ == "__main__":
    main()
