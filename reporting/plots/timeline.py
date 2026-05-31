from typing import Any

import plotly.graph_objects as go


def build_timeline_plot(report: dict[str, Any]) -> dict[str, Any]:
    segments = report.get("phase_segments", [])
    fig = go.Figure()
    for index, segment in enumerate(segments):
        fig.add_trace(
            go.Scatter(
                x=[segment.get("ts_start"), segment.get("ts_end")],
                y=[index, index],
                mode="lines+markers",
                name=segment.get("phase"),
            )
        )
    fig.update_layout(title="Session Phase Timeline", xaxis_title="Time", yaxis_title="Phase index")
    return fig.to_dict()

