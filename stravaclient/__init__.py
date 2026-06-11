"""Local replication and analysis layer for the Strava client.

The original analyzer modules (strava_client, zone_analyzer, workout_detector)
live in the project root; make sure the root is importable even when this
package is run from elsewhere.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
