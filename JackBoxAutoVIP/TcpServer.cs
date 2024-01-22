using System.Net;
using System.Net.Sockets;
using System.Text;

namespace JackBoxAutoVIP;

// Just a simple TCP server that listens for a connection and sends back the room code.
public class TcpServer
{
    private readonly TcpListener _server;
    private List<TcpConnectedClient> _clients;
    private bool _isRunning;
    private string _roomCode;

    public TcpServer(int port)
    {
        _server = new TcpListener(IPAddress.Any, port);
        _clients = new List<TcpConnectedClient>();
        _roomCode = "";
        _server.Start();
        _isRunning = true;
    }

    public async void Listen()
    {
        while (_isRunning)
        {
            var client = await _server.AcceptTcpClientAsync();
            ConnectClient(client);
        }
    }

    public void ConnectClient(TcpClient client)
    {
        Console.WriteLine($"device connected: {client.Client.RemoteEndPoint}");
        var tcpConnectedClient = new TcpConnectedClient(client);
        _clients.Add(tcpConnectedClient);
        
        // Send the current roomCode to new client
        tcpConnectedClient.SendRoomCode(_roomCode);
    }

    // Send the current roomCode to all connected clients
    public void SetRoomCode(string roomCode)
    {
        if (roomCode != _roomCode)
        {
            _roomCode = roomCode;
            
            // Create a copy of clients so we can remove old collections from to main list
            var clients = _clients.ToArray();
            
            foreach (var client in clients)
            {
                if (client.IsConnected())
                {
                    client.SendRoomCode(roomCode);
                }
                else
                {
                    // Remove client from the list and let the c# garbage collector clean up after it because im too lazy
                    _clients.Remove(client);
                }
                
            }
        }
        
    }
    
}