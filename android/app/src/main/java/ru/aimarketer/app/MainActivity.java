package ru.aimarketer.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.text.InputType;
import android.view.ViewGroup;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.LinearLayout;

/**
 * Оболочка приложения: показывает кабинет AI-маркетолога.
 *
 * Адрес спрашивается один раз и запоминается. Так одно и то же приложение
 * работает с любым запущенным кабинетом: на компьютере в Wi-Fi или на сервере.
 */
public class MainActivity extends Activity {

    private WebView web;
    private SharedPreferences prefs;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("app", MODE_PRIVATE);

        web = new WebView(this);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        web.setWebViewClient(new WebViewClient());
        setContentView(web);

        String address = prefs.getString("url", "");
        if (address.isEmpty()) {
            askAddress();
        } else {
            web.loadUrl(address);
        }
    }

    private void askAddress() {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setHint("http://192.168.1.10:8081");
        LinearLayout box = new LinearLayout(this);
        box.setPadding(48, 24, 48, 0);
        box.addView(input, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        new AlertDialog.Builder(this)
                .setTitle("Адрес кабинета")
                .setMessage("Введите адрес, который показывает приложение на компьютере. "
                        + "Телефон и компьютер должны быть в одной сети Wi-Fi.")
                .setView(box)
                .setPositiveButton("Открыть", (dialog, which) -> {
                    String value = input.getText().toString().trim();
                    if (!value.startsWith("http")) {
                        value = "http://" + value;
                    }
                    prefs.edit().putString("url", value).apply();
                    web.loadUrl(value);
                })
                .setNegativeButton("Отмена", null)
                .show();
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
