package dev.droidpilot.ime;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.inputmethodservice.InputMethodService;
import android.os.Build;
import android.text.InputType;
import android.util.Base64;
import android.view.KeyEvent;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;

import java.nio.charset.StandardCharsets;

/**
 * Commits text sent by `dp type` (any language, no conversion by the user's keyboard).
 *
 *   am broadcast -a dev.droidpilot.ime.TEXT -p dev.droidpilot.ime --es b64 BASE64 [--ez clear true] [--ez enter true]
 *
 * Result code: 1 typed, 2 no text field has focus, 3 bad request.
 * Only senders holding WRITE_SECURE_SETTINGS (adb shell has it; ordinary apps cannot get it) are heard,
 * and only while this keyboard is the active one.
 */
public final class DpIme extends InputMethodService {
    static final String ACTION = "dev.droidpilot.ime.TEXT";
    static final String SENDER_PERMISSION = "android.permission.WRITE_SECURE_SETTINGS";

    private final BroadcastReceiver receiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            setResultCode(handle(intent));
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        IntentFilter filter = new IntentFilter(ACTION);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(receiver, filter, SENDER_PERMISSION, null, Context.RECEIVER_EXPORTED);
        } else {
            registerReceiver(receiver, filter, SENDER_PERMISSION, null);
        }
    }

    @Override
    public void onDestroy() {
        unregisterReceiver(receiver);
        super.onDestroy();
    }

    // No keyboard panel: the app keeps its full height, so screenshots look as they would with a pen.
    @Override
    public boolean onEvaluateInputViewShown() {
        return false;
    }

    @Override
    public boolean onEvaluateFullscreenMode() {
        return false;
    }

    private int handle(Intent intent) {
        String b64 = intent.getStringExtra("b64");
        if (b64 == null) return 3;
        String text;
        try {
            text = new String(Base64.decode(b64, Base64.DEFAULT), StandardCharsets.UTF_8);
        } catch (IllegalArgumentException e) {
            return 3;
        }
        InputConnection ic = getCurrentInputConnection();
        EditorInfo info = getCurrentInputEditorInfo();
        if (ic == null || info == null || info.inputType == InputType.TYPE_NULL) return 2;
        ic.beginBatchEdit();
        if (intent.getBooleanExtra("clear", false)) {
            ic.commitText("", 1);  // drops any selection
            ic.deleteSurroundingText(1_000_000, 1_000_000);
        }
        if (!text.isEmpty()) ic.commitText(text, 1);
        ic.endBatchEdit();
        if (intent.getBooleanExtra("enter", false) && !sendDefaultEditorAction(true)) {
            sendDownUpKeyEvents(KeyEvent.KEYCODE_ENTER);
        }
        return 1;
    }
}
