"""Step 5. Count both systems day by day and say, in one line, whether everything arrived."""

from __future__ import annotations

from ..widgets import Page, empty
from . import result


class CheckPage(Page):
    def __init__(self, app):
        super().__init__(
            app,
            "Check the result",
            "Counts the logs on both systems, day by day, and compares them.",
        )

    def refresh(self) -> None:
        state = self.app.state
        if self.app.owner("check") or self.app.busy("verify"):
            self.set_body(self.activity)
            self.set_footer("Counting only reads. Nothing is changed on either system.")
            return
        copy = state.copy()
        if copy is None:
            self.set_body(
                empty("Nothing to check yet", "Start a copy first. Its result is checked here."),
                fill=True,
            )
            self.set_footer("")
            return
        view, found = result.describe(self.app, copy, past=False)
        self.view = view
        self.set_body(*result.widgets(self.app, copy, view, found))
        self.set_footer(view["hint"], result.actions(self.app, copy, view))
