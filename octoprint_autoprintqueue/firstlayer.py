"""Dependency-free first-layer G-code parser."""
import math
import re

_cmd_re = re.compile(r'^\s*(?:N\d+\s+)?([GgMmT]\d*)(.*)')
_param_re = re.compile(r'([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))')


def _strip_comment(line):
    return line.split(';', 1)[0]


def _is_marker(raw_stripped):
    s = raw_stripped.upper()
    if s.startswith(';LAYER:'):
        try:
            float(s[7:])
            return True
        except ValueError:
            return False
    if s == ';LAYER_CHANGE':
        return True
    return False


def parse_first_layer(lines, max_lines=400000, min_step=0.2):
    x = y = z = e = 0.0
    tool = 0
    abs_pos = True
    m83_active = False

    method = "none"
    first_z = None
    marker_seen = 0
    had_extrusion = False
    lines_read = 0
    truncated = False
    stop = False
    segment_count = 0

    polylines = {}
    current_polyline = None

    def _start_poly():
        nonlocal current_polyline
        if current_polyline is not None:
            pass
        polylines.setdefault(tool, [])
        current_polyline = [round(x, 2), round(y, 2)]
        polylines[tool].append(current_polyline)

    def _end_poly():
        nonlocal current_polyline
        current_polyline = None

    def _add_end(px, py):
        px = round(px, 2)
        py = round(py, 2)
        current_polyline.append(px)
        current_polyline.append(py)

    def _check_z_end():
        nonlocal stop, first_z, method
        if first_z is None:
            first_z = z
            return
        if z > first_z + 0.01:
            stop = True
            method = "z"

    for raw_line in lines:
        if lines_read >= max_lines:
            truncated = True
            break

        lines_read += 1
        raw_stripped = raw_line.strip()

        if not raw_stripped:
            continue

        # Check for layer marker on raw line
        if _is_marker(raw_stripped):
            if marker_seen == 0:
                marker_seen = 1
                method = "marker"
                first_z = None
                segment_count = 0
                had_extrusion = False
                polylines.clear()
                _end_poly()
                continue
            else:
                marker_seen = 2
                method = "marker"
                stop = True
                break

        line = _strip_comment(raw_line)
        stripped = line.strip()

        if not stripped:
            continue

        m = _cmd_re.match(stripped)
        if not m:
            continue

        cmd_word = m.group(1)
        rest = m.group(2)

        params = {}
        for pm in _param_re.finditer(rest):
            params[pm.group(1).upper()] = float(pm.group(2))

        cmd_upper = cmd_word.upper()

        # Tool change: command word is T<n>
        if cmd_upper.startswith('T') and len(cmd_word) > 1:
            tool = int(float(cmd_word[1:]))
            _end_poly()
            continue

        # G90/G91
        if cmd_upper == 'G90':
            abs_pos = True
            continue
        if cmd_upper == 'G91':
            abs_pos = False
            continue

        # M82/M83
        if cmd_upper == 'M82':
            m83_active = False
            continue
        if cmd_upper == 'M83':
            m83_active = True
            continue

        # G92
        if cmd_upper == 'G92':
            if 'X' in params:
                x = params['X']
            if 'Y' in params:
                y = params['Y']
            if 'Z' in params:
                z = params['Z']
            if 'E' in params:
                e = params['E']
            continue

        # G0/G1
        if cmd_upper in ('G0', 'G1'):
            nx, ny, nz = x, y, z
            ne = e
            e_rel = m83_active or not abs_pos

            if 'X' in params:
                nx = params['X'] if abs_pos else x + params['X']
            if 'Y' in params:
                ny = params['Y'] if abs_pos else y + params['Y']
            if 'Z' in params:
                nz = params['Z'] if abs_pos else z + params['Z']
            if 'E' in params:
                ne = params['E'] if not e_rel else e + params['E']

            delta_e = ne - e
            dx = nx - x
            dy = ny - y
            dist = math.sqrt(dx * dx + dy * dy)

            is_extr = delta_e > 1e-9 and dist > 1e-9

            if is_extr:
                if marker_seen == 0 and not stop:
                    z = nz
                    _check_z_end()
                    if stop:
                        break
                if first_z is None:
                    first_z = nz
                had_extrusion = True
                segment_count += 1
                if current_polyline is None:
                    _start_poly()
                _add_end(nx, ny)
            elif dist > 1e-9 or delta_e > 1e-9:
                _end_poly()
            x, y, z, e = nx, ny, nz, ne
            continue

        # G2/G3 arcs
        if cmd_upper in ('G2', 'G3'):
            cw = (cmd_upper == 'G2')
            nx, ny, nz = x, y, z
            ni, nj = 0.0, 0.0
            ne = e
            e_rel = m83_active or not abs_pos
            has_x = 'X' in params
            has_y = 'Y' in params
            has_ij = 'I' in params and 'J' in params

            if has_x:
                nx = params['X'] if abs_pos else x + params['X']
            if has_y:
                ny = params['Y'] if abs_pos else y + params['Y']
            if 'Z' in params:
                nz = params['Z'] if abs_pos else z + params['Z']
            if 'I' in params:
                ni = params['I']
            if 'J' in params:
                nj = params['J']
            if 'E' in params:
                ne = params['E'] if not e_rel else e + params['E']

            if not (has_x and has_y and has_ij):
                continue

            cx = x + ni
            cy = y + nj
            dx0, dy0 = x - cx, y - cy
            dx1, dy1 = nx - cx, ny - cy
            r = math.sqrt(dx0 * dx0 + dy0 * dy0)

            if r < 1e-9:
                x, y, z = nx, ny, nz
                e = ne
                continue

            start_a = math.atan2(dy0, dx0)
            end_a = math.atan2(dy1, dx1)

            if cw:
                total = start_a - end_a
            else:
                total = end_a - start_a
            if total < 0:
                total += 2 * math.pi

            if total < 1e-9:
                total = 2 * math.pi

            delta_e = ne - e
            num_seg = max(2, math.ceil(total / (5 * math.pi / 180)))

            if delta_e > 1e-9:
                if marker_seen == 0 and not stop:
                    z = nz
                    _check_z_end()
                    if stop:
                        break
                if first_z is None:
                    first_z = nz
                had_extrusion = True
                segment_count += 1
                if current_polyline is None:
                    _start_poly()

                for i in range(num_seg):
                    if cw:
                        a = start_a - (total * i / num_seg)
                    else:
                        a = start_a + (total * i / num_seg)
                    px = cx + r * math.cos(a)
                    py = cy + r * math.sin(a)
                    _add_end(px, py)

                _add_end(nx, ny)

            x, y, z = nx, ny, nz
            e = ne
            _end_poly()
            continue

    # Build result
    all_points = []
    tools_out = {}

    for t, polys in polylines.items():
        tool_str = str(t)
        kept = []
        for poly in polys:
            pts = list(zip(poly[0::2], poly[1::2]))
            if len(pts) < 2:
                continue
            simplified = [pts[0]]
            for p in pts[1:-1]:
                d = math.hypot(p[0] - simplified[-1][0], p[1] - simplified[-1][1])
                if d >= min_step:
                    simplified.append(p)
            simplified.append(pts[-1])
            flat = []
            for px, py in simplified:
                flat.append(round(px, 2))
                flat.append(round(py, 2))
            total_len = 0
            for i in range(0, len(flat) - 2, 2):
                total_len += math.hypot(flat[i+2] - flat[i], flat[i+3] - flat[i+1])
            if total_len >= 1e-6:
                kept.append(flat)
                all_points.extend(simplified)

        if kept:
            tools_out[tool_str] = kept

    if all_points:
        xs = [p[0] for p in all_points]
        ys = [p[1] for p in all_points]
        bounds = [round(min(xs), 2), round(min(ys), 2), round(max(xs), 2), round(max(ys), 2)]
    else:
        bounds = None

    z_out = round(first_z, 2) if first_z is not None else None

    if not had_extrusion:
        method = "none"

    return {
        "z": z_out,
        "tools": tools_out,
        "bounds": bounds,
        "segments": segment_count,
        "method": method,
        "truncated": truncated,
        "lines_read": lines_read,
    }
