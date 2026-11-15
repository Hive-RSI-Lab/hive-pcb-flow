"""Shared copper for the sense-pair mutants (#1077): a 5 mOhm shunt R931 between
KA and KB, fed on 2 mm rails from J931/J932, read by U931's IN+/IN- over two
hand-routed 0.2 mm lines that no sense_pads entry declares."""
import mutlib

DX, DY = 90, 104   # tests/test_kelvin.py's _pair_circuit, moved onto blinky2


def _u(base, n, k):
    return f"{base}-0000-4000-8000-{n:06d}{k:06d}"


def _fp(base, ref, value, x, y, n, pads):
    rows = "".join(
        f'\t\t(pad "{num}" smd rect\n\t\t\t(at {dx} 0)\n\t\t\t(size {s} {s})\n'
        f'\t\t\t(layers "F.Cu")\n\t\t\t(net "{net}")\n'
        + (f'\t\t\t(pinfunction "{fn}")\n' if fn else "")
        + f'\t\t\t(uuid "{_u(base, n, 100 + int(num))}")\n\t\t)\n'
        for num, dx, s, net, fn in pads)
    return (f'\t(footprint "t:SENSE"\n\t\t(layer "F.Cu")\n'
            f'\t\t(uuid "{_u(base, n, 1)}")\n\t\t(at {x + DX} {y + DY})\n'
            f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.Fab")\n'
            f'\t\t\t(uuid "{_u(base, n, 2)}")\n\t\t)\n'
            f'\t\t(property "Value" "{value}"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.Fab")\n'
            f'\t\t\t(uuid "{_u(base, n, 3)}")\n\t\t)\n'
            + mutlib.footprint_fields(_u(base, n, 9)[:35])
            + f'{rows}\t)\n')


def _poly(base, net, pts, w, n0):
    return "".join(
        f'\t(segment\n\t\t(start {a[0] + DX} {a[1] + DY})\n'
        f'\t\t(end {b[0] + DX} {b[1] + DY})\n\t\t(width {w})\n'
        f'\t\t(layer "F.Cu")\n\t\t(net "{net}")\n'
        f'\t\t(uuid "{_u(base, 9, n0 + i)}")\n\t)\n'
        for i, (a, b) in enumerate(zip(pts, pts[1:])))


def items(base, a_line, b_line):
    return (_fp(base, "R931", "5m", 30, 8, 1, [("1", -2, 2, "KA", ""),
                                               ("2", 2, 2, "KB", "")])
            + _fp(base, "J931", "CONN", 28, 2, 2, [("1", 0, 2, "KA", "")])
            + _fp(base, "J932", "CONN", 32, 2, 3, [("1", 0, 2, "KB", "")])
            + _fp(base, "U931", "INA240", 30, 26, 4,
                  [("1", -1, 1, "KA", "IN+"), ("2", 1, 1, "KB", "IN-")])
            + _poly(base, "KA", [(28, 2), (28, 8)], 2, 1)
            + _poly(base, "KB", [(32, 2), (32, 8)], 2, 2)
            + _poly(base, "KA", a_line, 0.2, 10)
            + _poly(base, "KB", b_line, 0.2, 20))


APART = ([(28, 8), (28, 12), (20, 12), (20, 26), (29, 26)],
         [(32, 8), (32, 12), (40, 12), (40, 26), (31, 26)])
TOGETHER = ([(28, 8), (28, 11), (29.6, 11), (29.6, 26), (29, 26)],
            [(32, 8), (32, 11), (30.4, 11), (30.4, 26), (31, 26)])


def insert(text, its):
    at = text.index("\t(segment\n")
    return text[:at] + its + text[at:]
