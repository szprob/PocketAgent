package com.pocketagent.mobile;

/**
 * 后端走到哪一步了。服务和界面在同一个进程里：服务往这里写，界面轮询时读。
 * 进程被系统杀掉后回到 IDLE，界面按「还没起来」重新拉起服务。
 */
final class BackendStatus {
    enum Phase {
        /** 这个进程里还没启动过。 */
        IDLE,
        /** 正在解压、安装或起后端。 */
        STARTING,
        /** 后端进程在跑（刚起来的几秒里接口可能还没通）。 */
        RUNNING,
        /** 初始化失败，或者后端反复起不来；要用户点「重试」。 */
        FAILED,
        /** 用户在通知上点了「停止」；要用户点「启动后端」。 */
        STOPPED,
    }

    private static volatile Phase phase = Phase.IDLE;
    private static volatile String message = "";
    private static volatile boolean interfaceVisible;

    private BackendStatus() {
    }

    static void set(Phase newPhase, String newMessage) {
        message = newMessage;
        phase = newPhase;
    }

    static Phase phase() {
        return phase;
    }

    /** 给用户看的一句话；只有 FAILED / STOPPED 时有内容。 */
    static String message() {
        return message;
    }

    /** 界面在前台时不用发「等你确认 / 跑完了」的通知。 */
    static void setInterfaceVisible(boolean visible) {
        interfaceVisible = visible;
    }

    static boolean isInterfaceVisible() {
        return interfaceVisible;
    }
}
