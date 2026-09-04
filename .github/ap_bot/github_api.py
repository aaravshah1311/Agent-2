# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — GitHub REST API Wrapper.

Provides the ``GitHubAPI`` class which encapsulates all interactions with the
GitHub REST API used by the AP Bot system.  Every method includes structured
logging and graceful error handling so that transient failures never crash the
bot.
"""

from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from .config import GITHUB_API_BASE
from .logger import logger


class GitHubAPI:
    """Wrapper around the GitHub REST API.

    Manages authentication, session reuse, and provides typed convenience
    methods for every endpoint the AP Bot consumes.

    Attributes:
        token: GitHub personal access token or ``GITHUB_TOKEN``.
        repo: Repository in ``owner/repo`` format.
        session: Pre-configured :class:`requests.Session`.
    """

    def __init__(self, token: str, repo: str) -> None:
        """Initialise the GitHub API client.

        Args:
            token: GitHub authentication token.
            repo: Full repository name (``owner/repo``).
        """
        self.token: str = token
        self.repo: str = repo
        self.base_url: str = f"{GITHUB_API_BASE}/repos/{self.repo}"

        self.session: requests.Session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

        logger.info("GitHubAPI initialised for repo: %s", self.repo)

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    def get_issue(self, issue_number: int) -> Optional[Dict[str, Any]]:
        """Fetch a single issue by number.

        Args:
            issue_number: The issue number to retrieve.

        Returns:
            Issue data as a dictionary, or ``None`` on failure.
        """
        url = f"{self.base_url}/issues/{issue_number}"
        try:
            response = self.session.get(url)
            response.raise_for_status()
            logger.info("Fetched issue #%d successfully.", issue_number)
            return response.json()
        except requests.RequestException as exc:
            logger.error("Failed to fetch issue #%d: %s", issue_number, exc)
            return None

    def list_issues(
        self,
        state: str = "open",
        labels: Optional[str] = None,
        per_page: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """List issues for the repository.

        Args:
            state: Issue state filter (``open``, ``closed``, ``all``).
            labels: Comma-separated label names to filter by.
            per_page: Number of results per page (max 100).
            page: Page number for pagination.

        Returns:
            A list of issue dictionaries, or an empty list on failure.
        """
        url = f"{self.base_url}/issues"
        params: Dict[str, Any] = {
            "state": state,
            "per_page": per_page,
            "page": page,
        }
        if labels:
            params["labels"] = labels

        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            issues = response.json()
            logger.info(
                "Listed %d issues (state=%s, page=%d).",
                len(issues),
                state,
                page,
            )
            return issues
        except requests.RequestException as exc:
            logger.error("Failed to list issues: %s", exc)
            return []

    def add_comment(
        self, issue_number: int, body: str
    ) -> Optional[Dict[str, Any]]:
        """Add a comment to an issue or pull request.

        Args:
            issue_number: The issue/PR number to comment on.
            body: The comment body (Markdown supported).

        Returns:
            The created comment data, or ``None`` on failure.
        """
        url = f"{self.base_url}/issues/{issue_number}/comments"
        try:
            response = self.session.post(url, json={"body": body})
            response.raise_for_status()
            logger.info("Added comment to issue #%d.", issue_number)
            return response.json()
        except requests.RequestException as exc:
            logger.error(
                "Failed to add comment to issue #%d: %s", issue_number, exc
            )
            return None

    def add_labels(
        self, issue_number: int, labels: List[str]
    ) -> Optional[List[Dict[str, Any]]]:
        """Add labels to an issue or pull request.

        Args:
            issue_number: The issue/PR number to label.
            labels: List of label names to add.

        Returns:
            The updated label list, or ``None`` on failure.
        """
        url = f"{self.base_url}/issues/{issue_number}/labels"
        try:
            response = self.session.post(url, json={"labels": labels})
            response.raise_for_status()
            logger.info(
                "Added labels %s to issue #%d.", labels, issue_number
            )
            return response.json()
        except requests.RequestException as exc:
            logger.error(
                "Failed to add labels to issue #%d: %s", issue_number, exc
            )
            return None

    def remove_label(
        self, issue_number: int, label: str
    ) -> bool:
        """Remove a label from an issue or pull request.

        Args:
            issue_number: The issue/PR number.
            label: The label name to remove.

        Returns:
            ``True`` if the label was removed successfully, ``False`` otherwise.
        """
        encoded_label = quote(label, safe="")
        url = f"{self.base_url}/issues/{issue_number}/labels/{encoded_label}"
        try:
            response = self.session.delete(url)
            response.raise_for_status()
            logger.info(
                "Removed label '%s' from issue #%d.", label, issue_number
            )
            return True
        except requests.RequestException as exc:
            logger.error(
                "Failed to remove label '%s' from issue #%d: %s",
                label,
                issue_number,
                exc,
            )
            return False

    def close_issue(self, issue_number: int) -> Optional[Dict[str, Any]]:
        """Close an issue.

        Args:
            issue_number: The issue number to close.

        Returns:
            The updated issue data, or ``None`` on failure.
        """
        url = f"{self.base_url}/issues/{issue_number}"
        try:
            response = self.session.patch(url, json={"state": "closed"})
            response.raise_for_status()
            logger.info("Closed issue #%d.", issue_number)
            return response.json()
        except requests.RequestException as exc:
            logger.error("Failed to close issue #%d: %s", issue_number, exc)
            return None

    # ------------------------------------------------------------------
    # Pull Requests
    # ------------------------------------------------------------------

    def get_pull_request(self, pr_number: int) -> Optional[Dict[str, Any]]:
        """Fetch a single pull request by number.

        Args:
            pr_number: The pull request number.

        Returns:
            PR data as a dictionary, or ``None`` on failure.
        """
        url = f"{self.base_url}/pulls/{pr_number}"
        try:
            response = self.session.get(url)
            response.raise_for_status()
            logger.info("Fetched PR #%d successfully.", pr_number)
            return response.json()
        except requests.RequestException as exc:
            logger.error("Failed to fetch PR #%d: %s", pr_number, exc)
            return None

    def get_pr(self, pr_number: int) -> Optional[Dict[str, Any]]:
        """Fetch a single pull request by number. Alias for get_pull_request.

        Args:
            pr_number: The pull request number.

        Returns:
            PR data as a dictionary, or None on failure.
        """
        return self.get_pull_request(pr_number)


    def get_pr_diff(self, pr_number: int) -> Optional[str]:
        """Fetch the raw diff for a pull request.

        Args:
            pr_number: The pull request number.

        Returns:
            The diff as a string, or ``None`` on failure.
        """
        url = f"{self.base_url}/pulls/{pr_number}"
        headers = {"Accept": "application/vnd.github.diff"}
        try:
            response = self.session.get(url, headers=headers)
            response.raise_for_status()
            logger.info("Fetched diff for PR #%d successfully.", pr_number)
            return response.text
        except requests.RequestException as exc:
            logger.error(
                "Failed to fetch diff for PR #%d: %s", pr_number, exc
            )
            return None

    def get_pr_files(
        self, pr_number: int
    ) -> List[Dict[str, Any]]:
        """List files changed in a pull request.

        Args:
            pr_number: The pull request number.

        Returns:
            A list of file change dictionaries, or an empty list on failure.
        """
        url = f"{self.base_url}/pulls/{pr_number}/files"
        try:
            response = self.session.get(url)
            response.raise_for_status()
            files = response.json()
            logger.info(
                "Fetched %d changed files for PR #%d.", len(files), pr_number
            )
            return files
        except requests.RequestException as exc:
            logger.error(
                "Failed to fetch files for PR #%d: %s", pr_number, exc
            )
            return []

    def create_review(
        self,
        pr_number: int,
        body: str,
        event: str = "COMMENT",
    ) -> Optional[Dict[str, Any]]:
        """Create a review on a pull request.

        Args:
            pr_number: The pull request number.
            body: The review body text.
            event: Review event type (``COMMENT``, ``APPROVE``,
                   ``REQUEST_CHANGES``).

        Returns:
            The created review data, or ``None`` on failure.
        """
        url = f"{self.base_url}/pulls/{pr_number}/reviews"
        payload: Dict[str, str] = {"body": body, "event": event}
        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            logger.info(
                "Created review on PR #%d (event=%s).", pr_number, event
            )
            return response.json()
        except requests.RequestException as exc:
            logger.error(
                "Failed to create review on PR #%d: %s", pr_number, exc
            )
            return None

    # ------------------------------------------------------------------
    # Repository
    # ------------------------------------------------------------------

    def get_repo_stats(self) -> Optional[Dict[str, Any]]:
        """Fetch repository metadata and statistics.

        Returns:
            Repository data as a dictionary, or ``None`` on failure.
        """
        try:
            response = self.session.get(self.base_url)
            response.raise_for_status()
            logger.info("Fetched repository stats for %s.", self.repo)
            return response.json()
        except requests.RequestException as exc:
            logger.error("Failed to fetch repo stats: %s", exc)
            return None

    def list_contributors(self) -> List[Dict[str, Any]]:
        """List repository contributors.

        Returns:
            A list of contributor dictionaries, or an empty list on failure.
        """
        url = f"{self.base_url}/contributors"
        try:
            response = self.session.get(url)
            response.raise_for_status()
            contributors = response.json()
            logger.info("Fetched %d contributors.", len(contributors))
            return contributors
        except requests.RequestException as exc:
            logger.error("Failed to list contributors: %s", exc)
            return []

    def get_user_pull_requests(
        self, username: str
    ) -> List[Dict[str, Any]]:
        """Search for merged pull requests by a specific user in this repo.

        Uses the GitHub Search API to find all merged PRs authored by
        *username* within the current repository.

        Args:
            username: The GitHub username to search for.

        Returns:
            A list of search result items, or an empty list on failure.
        """
        url = f"{GITHUB_API_BASE}/search/issues"
        query = (
            f"type:pr is:merged author:{username} repo:{self.repo}"
        )
        params: Dict[str, Any] = {
            "q": query,
            "per_page": 100,
            "sort": "created",
            "order": "desc",
        }
        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            items: List[Dict[str, Any]] = data.get("items", [])
            logger.info(
                "Found %d merged PRs by user '%s'.", len(items), username
            )
            return items
        except requests.RequestException as exc:
            logger.error(
                "Failed to search PRs for user '%s': %s", username, exc
            )
            return []
