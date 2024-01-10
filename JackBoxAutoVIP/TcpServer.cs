using System.Net;
using System.Net.Sockets;
using System.Text;

namespace JackBoxAutoVIP;

// Just a simple TCP server that listens for a connection and sends back the room code.
public class TcpServer
{
    private readonly TcpListener _server;
    private bool _isRunning;
    public string roomCode = "";

    public TcpServer(int port)
    {
        _server = new TcpListener(IPAddress.Any, port);
        _server.Start();
        _isRunning = true;
    }

    public async void Listen()
    {
        while (_isRunning)
        {
            var client = await _server.AcceptTcpClientAsync();
            _ = HandleClient(client);
        }
    }

    public async Task HandleClient(TcpClient client)
    {
        var buffer = new byte[4096];
        while (_isRunning)
        {
            Console.WriteLine($"device connected: {client.Client.RemoteEndPoint}");
            var stream = client.GetStream();
            var bytesRead = await stream.ReadAsync(buffer, 0, buffer.Length);
            if (bytesRead == 0)
            {
                break; // Connection closed by client.
            }
            byte[] byteData = Encoding.UTF8.GetBytes(roomCode);
            
            // Send back the room code
            await stream.WriteAsync(byteData, 0, byteData.Length);
        }
        client.Close();
    }
}