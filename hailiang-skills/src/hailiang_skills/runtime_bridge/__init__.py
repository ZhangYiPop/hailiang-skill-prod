__all__ = ["MainPlannerOrchestrator"]


def __getattr__(name: str):
    if name != "MainPlannerOrchestrator":
        raise AttributeError(name)
    from hailiang_skills.runtime_bridge.main_planner import MainPlannerOrchestrator

    return MainPlannerOrchestrator
