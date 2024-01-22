using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading.Tasks;
namespace JackBoxAutoVIP;

public class TcpConnectedClient
{
    private TcpClient _client;
    private Task _ListenTask;

    public TcpConnectedClient(TcpClient client)
    {
        _client = client;
        _ListenTask = new Task(Listen);
        _ListenTask.Start();
    }
    
    private async void Listen()
    {
        var buffer = new byte[4096];
        while (_client.Connected)
        {
            var stream = _client.GetStream();
            var bytesRead = await stream.ReadAsync(buffer, 0, buffer.Length);
            
            if (bytesRead == 0)
            {
                break;
            }
            
            // Parse client message
            var clientMessage = Encoding.UTF8.GetString(buffer, 0, bytesRead).Trim();
            Console.WriteLine($"Received message: {clientMessage}");
        }
        _client.Close();
    }

    public async void SendRoomCode(string roomCode)
    {
        SendMessage("RoomCode:" + roomCode);
    }

    public bool IsConnected()
    {
        return _client.Connected;
    }
    
    private async void SendMessage(string message)
    {
        var stream = _client.GetStream();
        var byteData = Encoding.UTF8.GetBytes(message);
        await stream.WriteAsync(byteData, 0, byteData.Length);
    }
}