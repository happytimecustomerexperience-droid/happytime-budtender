"""dashboard app URLs — ``/dashboard/`` prefix (14-P4 §3.5).

Every route is staff-only (the views carry ``@staff_member_required``). Named ``dash-*`` so the
templates + the flow-canvas JS resolve by name.
"""

from __future__ import annotations

from django.urls import path

from . import views

urlpatterns = [
    path("", views.overview, name="dash-overview"),
    path("analytics/", views.analytics_dashboard, name="dash-analytics"),
    # agents
    path("agents/", views.agent_config, name="dash-agents"),
    path("agents/<int:pk>/save", views.agent_save, name="dash-agent-save"),
    path("agents/<int:pk>/assist", views.agent_prompt_assist, name="dash-agent-assist"),
    path("agents/<int:pk>/publish", views.publish_assistant_one, name="dash-agent-publish"),
    path("agents/<slug:role>/", views.agent_detail, name="dash-agent-detail"),
    # flow canvas (config + docs only)
    path("flow/", views.flow_canvas, name="dash-flow"),
    path("flow/save", views.flow_save, name="dash-flow-save"),
    # KB + KB-source manager + reindex
    path("kb/", views.kb_manager, name="dash-kb"),
    path("kb/reindex", views.kb_reindex, name="dash-kb-reindex"),
    path("kb/<slug:kind>/", views.kb_source_list, name="dash-kb-source"),
    path("kb/<slug:kind>/new/", views.kb_row_new, name="dash-kb-row-new"),
    path("kb/row/<int:pk>/", views.kb_row_edit, name="dash-kb-row-edit"),
    path("kb/row/<int:pk>/delete", views.kb_row_delete, name="dash-kb-row-delete"),
    # specials / hours editor (the faq-spoken StoreFact subset; CRUD via kb-row, kind=store-fact)
    path("specials-hours/", views.specials_hours, name="dash-specials-hours"),
    # ranking weights
    path("weights/", views.weights_tuner, name="dash-weights"),
    # calls
    path("calls/", views.call_monitor, name="dash-calls"),
    path("calls/log/", views.call_log, name="dash-call-log"),
    path("calls/<int:pk>/", views.call_detail, name="dash-call-detail"),
    path("calls/<int:pk>/transcript", views.call_transcript, name="dash-call-transcript"),
    path("escalations/", views.escalation_review, name="dash-escalations"),
    # vendor callbacks
    path("vendor-callbacks/", views.vendor_queue, name="dash-vendor-queue"),
    path(
        "vendor-callbacks/<int:pk>/update", views.vendor_callback_update, name="dash-vendor-update"
    ),
    # publish to Vapi
    path("publish/", views.publish_page, name="dash-publish"),
    path("publish/run", views.publish_vapi, name="dash-publish-run"),
]
