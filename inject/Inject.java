package dp;

import android.os.SystemClock;
import android.view.InputEvent;
import android.view.MotionEvent;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.lang.reflect.Method;

/**
 * Sends touch and stylus events from one process, run with app_process from adb shell.
 *
 * `adb shell input motionevent` starts a new process for every point (about 0.3 s each) and gives
 * every event its own downTime, so ink canvases see no stroke. This keeps one downTime per gesture,
 * an even pace, pressure, and a second finger for pinches.
 *
 * Reads commands from stdin, one per line:
 *   src stylus|touch       dt MS (pause after each event, default 8)       wait MS
 *   down X Y [P]   move X Y [P]   up X Y [P]                  one pointer
 *   down2 X0 Y0 X1 Y1   move2 X0 Y0 X1 Y1   up2 X0 Y0 X1 Y1   two fingers
 * Coordinates are display pixels in the current rotation. Prints "ok N" or "error …".
 */
public final class Inject {
    private static final int SOURCE_TOUCHSCREEN = 0x1002;
    private static final int SOURCE_STYLUS = 0x4002;
    private static final int MODE_WAIT_FOR_FINISH = 2;

    private Object manager;
    private Method inject;
    private boolean stylus;
    private long dt = 8;
    private long downTime;
    private float pressure = 1f;
    private int sent;
    private int refused;

    public static void main(String[] args) {
        try {
            new Inject().run();
        } catch (Throwable t) {
            Throwable c = t.getCause() != null ? t.getCause() : t;
            System.out.println("error " + t.getMessage() + (c != t ? " (" + c + ")" : ""));
            System.exit(1);
        }
    }

    private void run() throws Exception {
        Class<?> c;
        try {
            c = Class.forName("android.hardware.input.InputManagerGlobal");  // Android 14+
        } catch (ClassNotFoundException e) {
            c = Class.forName("android.hardware.input.InputManager");
        }
        manager = c.getDeclaredMethod("getInstance").invoke(null);
        inject = c.getMethod("injectInputEvent", InputEvent.class, int.class);
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
        String line;
        int n = 0;
        while ((line = in.readLine()) != null) {
            n++;
            line = line.trim();
            if (line.isEmpty() || line.startsWith("#")) continue;
            try {
                step(line.split("\\s+"));
            } catch (Exception e) {
                throw new Exception("line " + n + " \"" + line + "\": " + e, e);
            }
        }
        System.out.println(refused == 0 ? "ok " + sent : "error " + refused + " of " + sent + " events refused");
    }

    private void step(String[] p) throws Exception {
        switch (p[0]) {
            case "src":
                stylus = p[1].equals("stylus");
                return;
            case "dt":
                dt = Long.parseLong(p[1]);
                return;
            case "wait":
                SystemClock.sleep(Long.parseLong(p[1]));
                return;
            case "down":
                downTime = SystemClock.uptimeMillis();
                send(MotionEvent.ACTION_DOWN, p, 1);
                return;
            case "move":
                send(MotionEvent.ACTION_MOVE, p, 1);
                return;
            case "up":
                send(MotionEvent.ACTION_UP, p, 1);
                return;
            case "down2":
                downTime = SystemClock.uptimeMillis();
                send(MotionEvent.ACTION_DOWN, p, 1);
                send(MotionEvent.ACTION_POINTER_DOWN | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT), p, 2);
                return;
            case "move2":
                send(MotionEvent.ACTION_MOVE, p, 2);
                return;
            case "up2":
                send(MotionEvent.ACTION_POINTER_UP | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT), p, 2);
                send(MotionEvent.ACTION_UP, p, 1);
                return;
            default:
                throw new IllegalArgumentException("unknown command");
        }
    }

    private void send(int action, String[] p, int count) throws Exception {
        if (count == 1 && p.length > 3) pressure = Float.parseFloat(p[3]);
        MotionEvent.PointerProperties[] props = new MotionEvent.PointerProperties[count];
        MotionEvent.PointerCoords[] coords = new MotionEvent.PointerCoords[count];
        for (int i = 0; i < count; i++) {
            props[i] = new MotionEvent.PointerProperties();
            props[i].id = i;
            props[i].toolType = stylus && count == 1 ? MotionEvent.TOOL_TYPE_STYLUS : MotionEvent.TOOL_TYPE_FINGER;
            coords[i] = new MotionEvent.PointerCoords();
            coords[i].x = Float.parseFloat(p[1 + 2 * i]);
            coords[i].y = Float.parseFloat(p[2 + 2 * i]);
            coords[i].pressure = count == 1 ? pressure : 1f;
            coords[i].size = 0.05f;
        }
        int source = stylus && count == 1 ? (SOURCE_STYLUS | SOURCE_TOUCHSCREEN) : SOURCE_TOUCHSCREEN;
        long now = SystemClock.uptimeMillis();
        MotionEvent ev = MotionEvent.obtain(downTime, now, action, count, props, coords,
                0, 0, 1f, 1f, 0, 0, source, 0);
        boolean ok = (Boolean) inject.invoke(manager, ev, MODE_WAIT_FOR_FINISH);
        ev.recycle();
        sent++;
        if (!ok) refused++;
        if (dt > 0) SystemClock.sleep(dt);
    }
}
