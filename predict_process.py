import threading
import queue
import time
import socket
import numpy as np
from ecg_process import ecg_queue
from data_udp import UDP_queue
# from test import feature_queue
# from test import UDP_queue, populate_queues

# ==============================
# 模拟 AI 模型
# ==============================
def predict(avg_features: np.ndarray):
    """
    模拟推理函数
    """
    return float(np.sum(avg_features))  # 这里替换为真实模型推理


# ==============================
# 推理与 TCP 发送线程
# ==============================
def tcp_send_thread(host="127.0.0.1", port=8888):
    """
    每分钟取特征，求平均值，推理，并通过 TCP 发送结果
    """
    # 初始化 TCP 客户端
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect((host, port))
    print(f"[TCP] 已连接 {host}:{port}")

    ecg_buffer = []  # 用于缓存 10 s的特征
    eye_buffer = []  # 用于缓存 10 s的特征
    buffer = []
    while True:
        try:
            # 从队列中取出特征（阻塞等待）
            time.sleep(10)
            while not ecg_queue.empty():
                ecg_features = ecg_queue.get(timeout=65)
                ecg_buffer.append(ecg_features)
            while not UDP_queue.empty():
                eye_features = UDP_queue.get(timeout=65)
                eye_buffer.append(eye_features)

            ecg_buffer = [list(buff.values()) for buff in ecg_buffer]
            if len(ecg_buffer) and len(eye_buffer):
                features1 = np.mean(ecg_buffer, axis=0)
                features2 = np.mean(eye_buffer, axis=0)
                ecg_buffer.clear()
                eye_buffer.clear()
                features = np.concatenate((features1, features2))
                buffer.append(features)
            if len(buffer) == 6:      # 积累 1分钟特征后，开始推理
                input_feature = np.mean(buffer, axis=0)
                result = int(predict(input_feature))
                # 通过 TCP 发送推理结果
                client.sendall(str(result).encode("utf-8"))
                print(f"[TCP] 已发送推理结果: {result}")
                # 清空缓存，进入下一个周期
                buffer.clear()
        except Exception as e:
            print(f"[ERROR] TCP 发送线程异常: {e}")
            break

    # client.close()


# ==============================
# 主程序入口
# ==============================
if __name__ == "__main__":
    t1 = threading.Thread(target=tcp_send_thread, daemon=True)
    # t2 = threading.Thread(target=populate_queues, daemon=True)
    t1.start()
    # t2.start()
    # 主线程保持运行
    while True:
        time.sleep(10)

# -*- coding: utf-8 -*-
"""
import socket
def tcp_server(host="127.0.0.1", port=8888):
    # 简单的 TCP 接收端

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind((host, port))
    server.listen(1)   # 允许一个客户端连接
    print(f"[TCP Server] 正在监听 {host}:{port} ...")

    conn, addr = server.accept()
    print(f"[TCP Server] 客户端已连接: {addr}")

    try:
        while True:
            data = conn.recv(1024)
            if not data:
                print("[TCP Server] 客户端断开连接")
                break
            result = data.decode('utf-8')
            print(f"[TCP Server] 收到推理结果: {result}")
    except Exception as e:
        print(f"[ERROR] {e}")
    finally:
        conn.close()
        server.close()

if __name__ == "__main__":
    tcp_server()
 """