using System;
using System.Diagnostics;
using System.ComponentModel;
using System.Net.Http.Headers;
using System.Runtime.InteropServices;
using System.Text;

namespace JackBoxAutoVIP // Note: actual namespace depends on the project name.
{
    internal class Program
    {
        private const int PROCESS_WM_READ = 0x0010;

        [DllImport("kernel32.dll")]
        public static extern bool ReadProcessMemory(int hProcess, Int64 lpBaseAddress, byte[] lpBuffer, int dwSize, ref int lpNumberOfBytesRead);

        
        static void Main(string[] args)
        {
            
            
            // Dictionary of game titles and their memory pointers.
            Dictionary<string, Int64[]> GameMemoryPointers = new Dictionary<string, Int64[]>();
            GameMemoryPointers.Add("The Jackbox Party Pack", new Int64[]{0x00E15620, 0x0, 0x88, 0x8, 0x10});
            GameMemoryPointers.Add("The Jackbox Party Pack 3", new Int64[]{0x00E15600, 0x0, 0x88, 0x8, 0x10});
            GameMemoryPointers.Add("The Jackbox Party Pack 5", new Int64[]{0x00E14600, 0x0, 0x88, 0x8, 0x10});
            
            // Start the TCP server on a different thread.
            var server = new TcpServer(38469);
            var task = new Task(server.Listen);
            task.Start();
            
            string roomCode = "";
            
            // Loop forever checking all processes running on the system. 
            // If one of one of the process running is a JackBox game we support then start reading the memory for the room code until that game closes.
            while (true)
            {
                Process[] processes = Process.GetProcesses();
                
                // Get all process names from the processes list.
                String[] processesNames = new String[processes.Length];
                for (int i = 0; i < processes.Length; i++)
                {
                    processesNames[i] = processes[i].ProcessName;
                }
                
                // Loop through all the games we have memory pointers for.
                foreach (var gameTitle in GameMemoryPointers.Keys)
                {
                    // If the game is not running continue to the next process.
                    if (!processesNames.Contains(gameTitle))
                        continue;
                    
                    // Print the name of the game that is running
                    Console.WriteLine($"Game {gameTitle} is running.");
                    
                    Process process = Process.GetProcessesByName(gameTitle)[0];
                    
                    // While the process is running, read the memory.
                    while (!process.HasExited)
                    {
                        String roomCodeInMemory = ReadStringFromPointer(process, GameMemoryPointers[gameTitle]);
                        
                        // Check if room code changed
                        if (roomCodeInMemory == roomCode)
                        {
                            Thread.Sleep(10);
                            continue;
                        }
                        
                        if (!IsCodeValid(roomCode))
                        {
                            Thread.Sleep(10);
                            continue;
                        }

                        
                        roomCode = roomCodeInMemory;
                        
                        
                        server.SetRoomCode(roomCode);
                        
                    }
                }
            }
        }
        
        // A simple function that reads a string from a multi-level pointer.
        public static string ReadStringFromPointer(Process process, Int64[] offsets)
        {
            Int32 processHandle = (Int32)process.Handle;
            
            int bytesRead = 0;
            var buffer = new byte[4];
            
            Int64 currentAddress = process.MainModule.BaseAddress;
            foreach (var offset in offsets)
            {
                currentAddress += offset;
                ReadProcessMemory(processHandle, currentAddress, buffer, buffer.Length, ref bytesRead);
                currentAddress = BitConverter.ToInt32(buffer, 0);
            }

            ReadProcessMemory(processHandle, currentAddress, buffer, buffer.Length, ref bytesRead);
            return Encoding.Default.GetString(buffer);
        }
        
        // Very basic check to see if the string is a valid room code.
        // We dont have a way to detect if the game is in the lobby or not, so we just check if the string is 4 characters long and all uppercase.
        // Its pretty unlikely that the pointer will point to a random 4 character string that is all uppercase so its probably good enough.
        // We then check if the room code is valid by making a request to the JackBox API.
        public static bool IsCodeValid(string code)
        {
            if (code.Length != 4)
            {
                return false;
            }

            foreach (var character in code)
            {
                if (!Char.IsLetter(character) || !Char.IsUpper(character))
                {
                    return false;
                }
            }
            
            HttpClient client = new HttpClient();
            client.DefaultRequestHeaders.Accept.Add(
                new MediaTypeWithQualityHeaderValue("application/json"));
            client.BaseAddress = new Uri($"https://ecast.jackboxgames.com/api/v2/rooms/{code}");
            HttpResponseMessage response = client.GetAsync("").Result;
            if (!response.IsSuccessStatusCode)
            {
                return false;
            }

            return true;
        }
    }
}