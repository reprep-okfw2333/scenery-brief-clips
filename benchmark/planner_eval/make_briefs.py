"""Build varied valid briefs from the military template for planner evaluation."""
import json, copy
from pathlib import Path
from scenery_brief_clips.brief import validate_brief
src = json.load(open('benchmark/military/brief.json'))
cases = [
    ("horses", "grazing in a meadow", None, "three 720p clips of horses grazing in a meadow"),
    ("Brazilian military", "marching in a parade", None, "two 720p clips of the Brazilian military marching in a parade"),
    ("ocean waves", "crashing on rocks", None, "two 720p clips of ocean waves crashing on rocks"),
    ("red deer", "walking through a misty forest", None, "two 720p clips of red deer walking through a misty forest"),
    ("wolf", "running through snow", None, "two 720p clips of a wolf running through snow"),
    ("goose", "flying over a lake", None, "two 720p clips of a goose flying over a lake"),
    ("lighthouse", "in a storm", None, "two 720p clips of a lighthouse in a storm"),
    ("alpacas", "grazing on a hillside", None, "two 720p clips of alpacas grazing on a hillside"),
    ("tram", "climbing a steep street in Lisbon", None, "two 720p clips of a tram climbing a steep street in Lisbon"),
]
out = Path('tmp/planner_eval/briefs'); out.mkdir(parents=True, exist_ok=True)
for noun, action, setting, req in cases:
    b = copy.deepcopy(src)
    b['request_text'] = req
    b['scene']['subjects'] = [{"noun": noun, "min_visible": 1}]
    b['scene']['action'] = action
    b['theme_text'] = f"{noun} {action}"
    b['n_clips'] = 3 if req.startswith('three') else 2
    for v in b['sources'].values():
        if v['origin'] == 'user_clarification':
            v['quote'] = req
    validate_brief(b)
    (out / f"{noun.replace(' ', '_')}.json").write_text(json.dumps(b, indent=2))
print(len(cases), 'briefs')
